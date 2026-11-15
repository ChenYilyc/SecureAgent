import io
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app import answer_construction as qa
from app import document_control as control, search_construction as finder, workbench as web
from app import workbench_accounts as accounts
from app.main import app
from app.office_sources import parse_office

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SAMPLE = EXAMPLES / "workbench" / "QL-WB-101_R02.pdf"
QA_ENV = {"DEEPSEEK_API_KEY": "fixture", "SECUREAGENT_ALLOW_REMOTE_QA": "1", "SECUREAGENT_QA_PROJECTS": "QL01"}


def word_bytes():
    doc = Document()
    doc.add_paragraph("资料移交流程", "Title")
    doc.add_paragraph("2.1 归档时限", "Heading 2")
    doc.add_paragraph("项目资料员在接收方签收后 3 个工作日内归档移交清单。")
    doc.add_paragraph("2.2 清单内容", "Heading 2")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "签收人"
    table.cell(0, 1).text = "签收日期"
    data = io.BytesIO()
    doc.save(data)
    return data.getvalue()


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.index = self.root / "chunks.jsonl"
        self.enterContext(patch.multiple(control, ROOT=self.root, REGISTER=self.root/"registry.db",
                                        ARCHIVE=self.root/"archive", OUTPUT=self.index))
        self.enterContext(patch.object(finder, "CHUNKS_PATH", self.index))
        self.enterContext(patch.dict("os.environ", {
            "SECUREAGENT_RETRIEVAL": "lexical", "SECUREAGENT_ALLOW_REMOTE_QA": "0",
            "SECUREAGENT_QA_PROJECTS": "", "DEEPSEEK_API_KEY": "",
        }))
        with closing(accounts.connect()) as db, db:
            for role, project in (("clerk", "QL01"), ("reviewer", "QL01"), ("employee", "QL01"), ("employee", "HT02")):
                accounts.add(db, role+project, "fixture-password-only", role, [project])
        self.client = self.enterContext(TestClient(app))
        self.base = "/workbench/api/"

    def login(self, role, project="QL01"):
        self.client.headers.pop("x-wb-csrf", None)
        res = self.client.post(self.base+"login", json={"login":role+project,"password":"fixture-password-only"},
                               headers={"x-wb-login":"1"})
        self.assertEqual(res.status_code, 200)
        self.client.headers["x-wb-csrf"] = self.client.get(self.base+"me").json()["csrf"]

    def upload(self, raw, ext="docx", rev="R01"):
        return self.client.post(self.base+"QL01/upload", params={"document":"QL-WB-101","revision":rev,"format":ext},
                                content=raw, headers={"content-type":"application/octet-stream"})

    def endpoint(self, rev, action):
        return self.base+"QL01/QL-WB-101/"+rev+"/"+action

    def approval(self, rev):
        preview = self.client.get(self.endpoint(rev,"preview")).json()
        return dict(expected_current=preview["current"], sha256=preview["sha256"])

    def publish_qa_examples(self):
        filing = (EXAMPLES / "construction" / "QL-DOC-007_R01.md").read_bytes()
        custody = (EXAMPLES / "workbench" / "QL-ARC-014_R01.md").read_bytes()
        with closing(accounts.connect()) as db:
            with control.transaction(db):
                for project, doc, rev, raw, status in (
                    ("QL01", "QL-DOC-007", "R01", filing, "effective"),
                    ("QL01", "QL-ARC-014", "R01", custody, "effective"),
                    ("QL01", "QL-ARC-014", "R02", custody, "draft"),
                    ("HT02", "HT-ARC-014", "R01", custody, "effective"),
                ):
                    control.record_version(db, (project, doc, rev), raw, status, "fixture")
            control.publish(db)

    def test_real_word_and_pdf_approval_source_links_and_employee_scope(self):
        raw = word_bytes()
        self.login("clerk")
        self.assertTrue(self.upload(raw).json()["created"])
        self.assertFalse(self.upload(raw).json()["created"])
        self.assertEqual(self.upload(SAMPLE.read_bytes(),"pdf","R02").status_code, 200)
        p = self.client.get(self.endpoint("R01","preview")).json()
        self.assertEqual(p["sections"][0]["source_block"], 2)
        self.assertIn("签收日期", p["sections"][1]["text"])
        self.assertEqual(self.client.post(self.endpoint("R01","approve"),json=self.approval("R01")).status_code, 403)
        self.login("employee")
        self.assertEqual(self.client.get(self.base+"QL01/versions").json(), [])
        self.assertEqual(self.client.get(self.endpoint("R01","original")).status_code, 404)
        self.login("reviewer")
        stale = self.approval("R02")
        first = self.approval("R01")
        # The server records the logged-in user and rejects a client-supplied reviewer field.
        self.assertEqual(self.client.post(self.endpoint("R01","approve"),json={**first,"reviewer":"other"}).status_code, 422)
        self.assertEqual(self.client.post(self.endpoint("R01","approve"),json=first).status_code, 200)
        self.assertEqual(self.client.post(self.endpoint("R02","approve"),json=stale).status_code, 409)
        r = self.client.get(self.endpoint("R01","original"))
        self.assertEqual(r.content,raw)
        self.assertIn("attachment",r.headers["content-disposition"])
        self.assertEqual(self.client.post(self.endpoint("R02","approve"),json=self.approval("R02")).status_code, 200)
        history = self.client.get(self.endpoint("R02","preview")).json()["events"]
        self.assertEqual(history[-1]["actor"],"reviewerQL01")
        self.login("employee")
        found = self.client.post(self.base+"QL01/search",json={"question":"移交清单归档时限"}).json()["results"]
        clause = next(r for r in found if r["section"]=="2.1")
        self.assertIn("1 个工作日",clause["text"])
        self.assertEqual(clause["source_page"],1)
        self.assertIn("section=2.1",clause["preview_url"])
        self.assertTrue(all(r["revision"]=="R02" for r in found))
        r=self.client.get(self.endpoint("R02","original"))
        self.assertEqual(r.content,SAMPLE.read_bytes())
        self.assertEqual(r.headers["content-type"],"application/pdf")
        self.assertEqual(self.client.get(self.endpoint("R01","preview")).status_code,404)
        self.assertEqual(self.client.get(self.endpoint("R01","original")).status_code,404)
        self.login("employee","HT02")
        for route in ("QL01/versions","QL01/QL-WB-101/R02/preview","QL01/QL-WB-101/R02/original"):
            self.assertEqual(self.client.get(self.base+route).status_code,404)
        self.assertEqual(self.client.post(self.base+"QL01/search",json={"question":"归档"}).status_code,404)

    def test_anonymous_csrf_logout_and_scan(self):
        self.assertEqual(self.client.get(self.base+"QL01/versions").status_code,401)
        self.login("clerk")
        csrf=self.client.headers.pop("x-wb-csrf")
        self.assertEqual(self.upload(word_bytes()).status_code,403)
        self.client.headers["x-wb-csrf"]=csrf
        pdf=PdfWriter()
        pdf.add_blank_page(width=200,height=200)
        data=io.BytesIO()
        pdf.write(data)
        res=self.upload(data.getvalue(),"pdf")
        self.assertEqual(res.status_code,422)
        self.assertIn("OCR",res.json()["detail"])
        self.assertEqual(self.client.get(self.base+"QL01/versions").json(),[])
        self.assertEqual(self.client.post(self.base+"logout").status_code,200)
        self.assertEqual(self.client.get(self.base+"me").status_code,401)

    def test_model_selection_uses_authorized_published_citations(self):
        self.login("clerk")
        self.upload(SAMPLE.read_bytes(),"pdf","R02")
        self.login("reviewer")
        self.client.post(self.endpoint("R02","approve"),json=self.approval("R02"))
        self.login("employee")
        with patch.dict("os.environ", QA_ENV):
            key="QL01/QL-WB-101/R02/2.1"
            with patch.object(web,"choose_clause",return_value=key) as selector:
                result=self.client.post(self.base+"QL01/search",json={"question":"移交清单归档时限","use_model":True})
                self.assertEqual(result.json()["selected"],key)
                self.assertIn(key,selector.call_args.args[1])
            with patch.object(web,"choose_clause",return_value=None):
                result=self.client.post(self.base+"QL01/search",json={"question":"归档地点","use_model":True})
                self.assertIsNone(result.json()["selected"])
            with patch.object(web,"choose_clause",return_value="other-project"):
                self.assertEqual(self.client.post(self.base+"QL01/search",json={"question":"归档","use_model":True}).status_code,502)

    def test_employee_question_automatically_cites_only_the_supported_part(self):
        self.publish_qa_examples()
        self.login("employee")
        first = "图纸会审纪要的纸质原件必须保管多少年？"
        second = "归档后，纸质原件由哪个岗位长期保管？"
        custody = "QL01/QL-ARC-014/R01/4.2"
        filing = "QL01/QL-DOC-007/R01/3.2"
        form = {"question": first + second}
        self.enterContext(patch.dict("os.environ", {**QA_ENV, "SECUREAGENT_QA_PROJECTS": " HT02, QL01 "}))
        self.assertEqual(self.client.get(self.base + "me").json()["qa_projects"], ["QL01"])
        with patch.object(qa, "_request_selection", return_value=qa.EvidenceByPart(citations={"1": [], "2": [custody]})) as model:
            local = self.client.post(self.base + "QL01/search", json=form)
            self.assertTrue(local.json()["results"])
            model.assert_not_called()
            response = self.client.post(self.base + "QL01/ask", json=form)
            model.assert_called_once()
            evidence = model.call_args.args[1]["evidence"]
            self.assertEqual(set(evidence), {filing, custody})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mode"], "answer")
            unknown, known = response.json()["answers"]
            self.assertEqual(unknown, {"question": first, "citations": []})
            self.assertEqual(known["question"], second)
            citation, = known["citations"]
            self.assertEqual(citation["chunk_id"], custody)
            self.assertEqual(citation["text"], evidence[custody])
            self.assertEqual(citation["location"], "第 3 行")
            self.assertIn("section=4.2", citation["preview_url"])

            model.return_value = qa.EvidenceByPart(citations={"1": [], "2": ["HT02/HT-ARC-014/R01/4.2"]})
            rejected = self.client.post(self.base + "QL01/ask", json=form)
            self.assertEqual(rejected.status_code, 502)
            self.assertNotIn("answers", rejected.json())

    def test_employee_in_a_local_only_project_gets_its_sources_without_a_model_call(self):
        self.publish_qa_examples()
        self.enterContext(patch.dict("os.environ", QA_ENV))
        model = self.enterContext(patch.object(qa, "_request_selection"))
        form = {"question": "图纸会审纪要原件应该由谁长期保管？"}
        route = self.base + "HT02/ask"
        self.assertEqual(self.client.post(route, json=form).status_code, 401)
        self.login("employee", "HT02")
        self.assertEqual(self.client.get(self.base + "me").json()["qa_projects"], [])
        self.assertEqual(self.client.post(self.base + "QL01/ask", json=form).status_code, 404)
        response = self.client.post(route, json=form)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "search")
        self.assertEqual([r["chunk_id"] for r in response.json()["results"]], ["HT02/HT-ARC-014/R01/4.2"])
        self.assertEqual(self.client.post(route, json={**form, "use_model": True}).status_code, 422)
        explicit = self.client.post(self.base + "HT02/search", json={**form, "use_model": True, "multi": True})
        self.assertEqual(explicit.status_code, 409)
        self.client.headers.pop("x-wb-csrf")
        self.assertEqual(self.client.post(route, json=form).status_code, 403)
        model.assert_not_called()

    def test_employee_question_uses_local_search_without_complete_project_configuration(self):
        self.publish_qa_examples()
        self.login("employee")
        model = self.enterContext(patch.object(qa, "_request_selection"))
        for key, value in (("SECUREAGENT_QA_PROJECTS", ""), ("SECUREAGENT_QA_PROJECTS", "QL010"),
                           ("SECUREAGENT_ALLOW_REMOTE_QA", "0"), ("DEEPSEEK_API_KEY", " ")):
            with self.subTest(key=key, value=value), patch.dict("os.environ", {**QA_ENV, key: value}):
                result = self.client.post(self.base + "QL01/ask", json={"question": "图纸会审纪要归档"})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["mode"], "search")
                self.assertTrue(result.json()["results"])
        model.assert_not_called()

    def test_multi_rechecks_every_joint_citation_after_a_revision_changes(self):
        self.publish_qa_examples()
        self.login("employee")
        custody = "QL01/QL-ARC-014/R01/4.2"
        filing = "QL01/QL-DOC-007/R01/3.2"

        def replace_revision(*args, **kwargs):
            with closing(accounts.connect()) as db:
                control.approve(db, ("QL01", "QL-ARC-014", "R02"), "fixture-reviewer", expected_current="R01")
                control.publish(db)
            return qa.EvidenceByPart(citations={"1": [filing, custody], "2": [filing]})

        self.enterContext(patch.dict("os.environ", QA_ENV))
        with patch.object(qa, "_request_selection", side_effect=replace_revision):
            response = self.client.post(self.base + "QL01/search", json={
                "question": "图纸会审纪要分别由谁归档和长期保管？由谁办理归档？", "use_model": True, "multi": True,
            })
        self.assertEqual(response.status_code, 200)
        joint, independent = response.json()["answers"]
        self.assertEqual(joint["citations"], [])
        self.assertEqual([row["chunk_id"] for row in independent["citations"]], [filing])

    def test_multi_checks_opt_in_project_and_empty_evidence_before_using_provider(self):
        self.login("employee")
        route = self.base + "QL01/search"
        form = {"question": "哪天归档？谁保管？", "use_model": True, "multi": True}
        model = self.enterContext(patch.object(qa, "_request_selection"))
        with patch.dict("os.environ", {"SECUREAGENT_ALLOW_REMOTE_QA": "0"}):
            self.assertEqual(self.client.post(route, json=form).status_code, 409)
        with patch.dict("os.environ", QA_ENV):
            self.assertEqual(self.client.post(route, json={**form, "use_model": False}).status_code, 422)
            self.assertEqual(self.client.post(self.base + "HT02/search", json=form).status_code, 404)
            response = self.client.post(route, json=form)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["answers"], [
                {"question": "哪天归档？", "citations": []}, {"question": "谁保管？", "citations": []},
            ])
        model.assert_not_called()

    def test_distinct_pdf_fragments_remain_identifiable(self):
        title, sections = parse_office(SAMPLE.read_bytes(),".pdf","example.pdf","QL-WB-101")
        self.assertEqual(title,"资料移交流程")
        self.assertEqual(len({s["fragment_id"] for s in sections}), len(sections))
        self.assertTrue(all(s["source_page"]==1 for s in sections))

    def test_word_inline_article_keeps_its_body(self):
        doc = Document()
        doc.add_paragraph("第一条 移交资料须在签收后归档。")
        doc.add_paragraph("第二条 接收人须签字。")
        stream = io.BytesIO()
        doc.save(stream)
        _, sections = parse_office(stream.getvalue(), ".docx", "sample.docx", "DOC-01")
        self.assertEqual([(r["section"], r["source_block"]) for r in sections], [("一", 1), ("二", 2)])
        self.assertEqual(sections[0]["text"], "移交资料须在签收后归档。")


if __name__ == "__main__":
    unittest.main()
