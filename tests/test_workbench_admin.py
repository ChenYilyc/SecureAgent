from contextlib import closing, contextmanager
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import dotenv_values
from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row

from app import document_control as control, workbench_accounts as accounts
from app import search_construction as finder, workbench as web
from app import workbench_admin as admin, workbench_auth as auth
from app.main import app


API = "/workbench/api"
ROOT = Path(__file__).resolve().parents[1]


class AdminPermissionTests(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.operator = {"login": "test-admin", "is_company_admin": True, "csrf": "test-csrf"}
        self.identity = self.enterContext(patch.object(auth, "session", return_value=self.operator))
        self.form = {"login": "test-user", "password": secrets.token_urlsafe(18),
                     "project_id": "TEST", "role": "clerk"}

    def test_only_company_admins_can_list_and_create(self):
        with patch.object(admin, "directory") as directory, patch.object(admin, "create_user") as create:
            self.identity.return_value = None
            self.assertEqual(self.client.get(API + "/admin/users").status_code, 401)
            for role in accounts.ROLES:
                with self.subTest(role=role):
                    self.identity.return_value = {
                        "login": "ordinary", "role": role, "is_company_admin": False, "csrf": "user-csrf"
                    }
                    self.assertEqual(self.client.get(API + "/admin/users").status_code, 403)
                    response = self.client.post(API + "/admin/users", json=self.form,
                                                headers={"x-wb-csrf": "user-csrf"})
                    self.assertEqual(response.status_code, 403)
            directory.assert_not_called()
            create.assert_not_called()

    def test_all_admin_writes_require_csrf(self):
        with patch.object(admin, "create_user") as create_user, patch.object(admin, "create_project") as create_project:
            for route, body in (("users", self.form), ("projects", {"project_id": "P1", "display_name": "工程"})):
                with self.subTest(route=route):
                    response = self.client.post(API + "/admin/" + route, json=body)
                    self.assertEqual(response.status_code, 403)
            create_user.assert_not_called()
            create_project.assert_not_called()

    def test_invalid_grants_and_password_validation_do_not_echo_the_password(self):
        with patch.object(admin, "create_user") as create:
            for override in ({"is_company_admin": True}, {"role": "admin"}, {"password": "too-short"}):
                with self.subTest(field=next(iter(override))):
                    body = {**self.form, **override}
                    response = self.client.post(API + "/admin/users", json=body,
                                                headers={"x-wb-csrf": self.operator["csrf"]})
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn(body["password"], response.text)
            create.assert_not_called()


    def test_lifecycle_requires_admin_and_csrf(self):
        body = {"login": "target", "expected_revision": 1, "project_roles": {}}
        params = {"login": "target", "expected_revision": 1}
        with patch.object(admin, "update_memberships") as update, patch.object(admin, "delete_user") as delete:
            for identity, headers, status in (
                (None, {}, 401),
                ({"login": "staff", "csrf": "staff-csrf", "is_company_admin": False}, {"x-wb-csrf": "staff-csrf"}, 403),
                (self.operator, {}, 403),
            ):
                self.identity.return_value = identity
                self.assertEqual(self.client.put(API + "/admin/users/memberships", json=body,
                                                 headers=headers).status_code, status)
                self.assertEqual(self.client.delete(API + "/admin/users", params=params,
                                                    headers=headers).status_code, status)
            update.assert_not_called()
            delete.assert_not_called()

    def test_archiving_and_deleting_projects_require_admin_and_csrf(self):
        route = API + "/admin/projects/TEST"
        with patch.object(admin, "change_project_state") as change:
            for identity, headers, status in (
                (None, {}, 401),
                ({"login": "staff", "is_company_admin": False, "csrf": "c"}, {"x-wb-csrf": "c"}, 403),
                (self.operator, {}, 403),
            ):
                self.identity.return_value = identity
                params = {"expected_revision": 1}
                self.assertEqual(self.client.post(route + "/archive", params=params, headers=headers).status_code, status)
                self.assertEqual(self.client.delete(route, params=params, headers=headers).status_code, status)
            change.assert_not_called()

    def test_new_accounts_require_real_project_roles_and_one_input_format(self):
        user = {"login": "new-user", "password": self.form["password"]}
        headers = {"x-wb-csrf": self.operator["csrf"]}
        with patch.object(admin, "create_user") as create:
            for grants in (
                {"project_roles": {}},
                {"project_roles": {"A": "clerk", "B": "admin"}},
                {"project_roles": {"A": "clerk"}, "project_id": "B", "role": "reviewer"},
            ):
                response = self.client.post(API + "/admin/users", json={**user, **grants}, headers=headers)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(user["password"], response.text)
            create.assert_not_called()


@unittest.skipUnless(os.getenv("SECUREAGENT_TEST_LOCAL_PG") == "1", "Local PostgreSQL test is opt-in")
class LocalAdminFlowTests(unittest.TestCase):
    def setUp(self):
        dsn = dotenv_values(ROOT / ".env").get("SECUREAGENT_DATABASE_URL") or os.getenv("SECUREAGENT_DATABASE_URL")
        if not dsn or urlsplit(dsn).hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.fail("本地集成测试需要 .env 中的本机 PostgreSQL Database URL。")
        try:
            db = psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row)
        except psycopg.Error as exc:
            raise AssertionError(f"本地测试数据库连接失败（{type(exc).__name__}），请检查 Supabase 状态。") from None
        self.addCleanup(db.close)
        self.enterContext(db.transaction(force_rollback=True))
        self.db = db
        self.enterContext(patch.dict(os.environ, {"SECUREAGENT_DATABASE_URL": dsn, "SECUREAGENT_DOCUMENT_BACKEND": "sqlite"}))

        # Each service call gets a savepoint. The outer transaction discards every test row.
        @contextmanager
        def using_test_db(_dsn):
            with db.transaction():
                yield db

        self.enterContext(patch.object(auth, "postgres_connection", using_test_db))
        self.client = self.enterContext(TestClient(app))
        suffix = uuid4().hex
        self.actor = "a-" + suffix
        self.project = "P" + suffix
        self.login = "u-" + suffix
        self.password = secrets.token_urlsafe(18)
        salt = secrets.token_bytes(16)
        db.execute(
            """INSERT INTO secureagent.wb_accounts (login, salt, digest, is_company_admin)
               VALUES (%s, %s, %s, TRUE)""",
            (self.actor, salt, accounts.password_digest(self.password, salt)),
        )

        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch.multiple(control, ROOT=root, REGISTER=root / "registry.sqlite3",
                                         ARCHIVE=root / "archive", OUTPUT=root / "chunks.jsonl"))
        self.enterContext(patch.object(finder, "CHUNKS_PATH", control.OUTPUT))
        self.enterContext(patch.dict(os.environ, {"SECUREAGENT_RETRIEVAL": "lexical", "SECUREAGENT_ALLOW_REMOTE_QA": "0"}))
        with closing(accounts.connect()) as registry, registry:
            registry.execute(
                """INSERT INTO versions (project_id, document_id, revision, title, status, sha256, chunks)
                   VALUES (?, 'LEGACY', 'R01', '旧台账', 'draft', ?, '[]')""", ("L" + suffix, "0" * 64),
            )
        self.legacy_project = "L" + suffix
        self.sign_in(self.actor, self.password)

    def sign_in(self, login, password, client=None):
        if client is None:
            client = self.client
        response = client.post(API + "/login", json={"login": login, "password": password},
                               headers={"x-wb-login": "1"})
        self.assertEqual(response.status_code, 200)
        profile = client.get(API + "/me")
        self.assertEqual(profile.status_code, 200)
        client.headers["x-wb-csrf"] = profile.json()["csrf"]
        return profile.json()

    def test_provision_a_clerk_with_one_project_and_audited_writes(self):
        project_body = {"project_id": self.project, "display_name": "本地测试工程"}
        legacy = self.client.post(API + "/admin/projects", json={**project_body, "project_id": self.legacy_project})
        self.assertEqual(legacy.status_code, 409)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM secureagent.projects WHERE project_id = %s", (self.legacy_project,)
        ).fetchone())

        created_project = self.client.post(API + "/admin/projects", json=project_body)
        self.assertEqual(created_project.status_code, 201)
        self.assertEqual(self.client.post(API + "/admin/projects", json=project_body).status_code, 409)
        self.assertNotIn(self.project, self.client.get(API + "/me").json()["projects"])

        initial_password = secrets.token_urlsafe(18)
        body = {"login": self.login, "password": initial_password, "project_id": self.project, "role": "clerk"}
        missing = self.client.post(API + "/admin/users", json={**body, "project_id": self.project + "x"})
        self.assertEqual(missing.status_code, 404)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM secureagent.wb_accounts WHERE login = %s", (self.login,)
        ).fetchone())

        created_user = self.client.post(API + "/admin/users", json=body)
        self.assertEqual(created_user.status_code, 201)
        self.assertNotIn(initial_password, created_user.text)
        self.assertEqual(created_user.json(), {"login": self.login, "project_id": self.project, "role": "clerk"})
        duplicate = self.client.post(API + "/admin/users", json={**body, "role": "reviewer",
                                                                 "password": secrets.token_urlsafe(18)})
        self.assertEqual(duplicate.status_code, 409)
        directory = self.client.get(API + "/admin/users")
        self.assertEqual(directory.status_code, 200)
        record = next(row for row in directory.json()["users"] if row["login"] == self.login)
        self.assertEqual(record, {"login": self.login, "is_active": True, "is_company_admin": False,
                                  "project_roles": {self.project: "clerk"}, "access_revision": 1})
        events = self.db.execute(
            """SELECT action, request_id FROM secureagent.admin_events
               WHERE actor_login = %s ORDER BY event_id""", (self.actor,)
        ).fetchall()
        self.assertEqual(events, [
            {"action": "project_created", "request_id": created_project.headers["x-request-id"]},
            {"action": "user_created", "request_id": created_user.headers["x-request-id"]},
        ])

        profile = self.sign_in(self.login, initial_password)
        self.assertEqual(profile["project_roles"], {self.project: "clerk"})
        self.assertFalse(profile["is_company_admin"])
        self.assertEqual(self.client.get(API + "/admin/users").status_code, 403)

    def multi_project_fixture(self):
        other = self.project + "B"
        roles = {self.project: "clerk", other: "employee"}
        for code in roles:
            response = self.client.post(API + "/admin/projects", json={"project_id": code, "display_name": code})
            self.assertEqual(response.status_code, 201)
        with closing(accounts.connect()) as registry, registry:
            for code in roles:
                registry.execute(
                    """INSERT INTO versions (project_id, document_id, revision, title, status, sha256, chunks)
                       VALUES (?, 'D1', 'R01', '测试草稿', 'draft', ?, '[]')""", (code, "0" * 64),
                )
        body = {"login": self.login, "password": secrets.token_urlsafe(18), "project_roles": roles}
        unknown = self.client.post(API + "/admin/users", json={**body, "project_roles": {**roles, other + "x": "clerk"}})
        self.assertEqual(unknown.status_code, 404)
        created = self.client.post(API + "/admin/users", json=body)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json(), {"login": self.login, "project_roles": roles})
        return body, other

    def set_roles(self, revision, roles, login=None):
        return self.client.put(API + "/admin/users/memberships", json={
            "login": login or self.login, "expected_revision": revision, "project_roles": roles,
        })

    def delete_account(self, revision, login=None):
        return self.client.delete(API + "/admin/users", params={
            "login": login or self.login, "expected_revision": revision,
        })

    def test_project_roles_change_independently_in_an_existing_session(self):
        body, other = self.multi_project_fixture()
        employee = self.enterContext(TestClient(app))
        profile = self.sign_in(self.login, body["password"], employee)
        self.assertEqual(profile["project_roles"], body["project_roles"])
        first = employee.get(API + f"/{self.project}/versions")
        second = employee.get(API + f"/{other}/versions")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.json()), 1)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), [])

        new_roles = {other: "reviewer"}
        self.assertEqual(self.set_roles(1, {**new_roles, other + "x": "employee"}).status_code, 404)
        self.assertEqual(employee.get(API + "/me").json()["project_roles"], body["project_roles"])
        self.assertEqual(self.set_roles(1, new_roles).json()["access_revision"], 2)
        self.assertEqual(self.set_roles(1, new_roles).json()["access_revision"], 2)
        self.assertEqual(self.set_roles(1, {self.project: "reviewer"}).status_code, 409)
        self.assertEqual(employee.get(API + f"/{self.project}/versions").status_code, 404)
        now_visible = employee.get(API + f"/{other}/versions")
        self.assertEqual(now_visible.status_code, 200)
        self.assertEqual([row["document_id"] for row in now_visible.json()], ["D1"])

        cleared = self.set_roles(2, {})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["access_revision"], 3)
        profile = employee.get(API + "/me")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["projects"], [])
        self.assertEqual(employee.get(API + f"/{other}/versions").status_code, 404)
        rows = self.db.execute(
            """SELECT roles_before, roles_after FROM secureagent.admin_events
               WHERE target_login = %s AND action = 'memberships_changed' ORDER BY event_id""", (self.login,)
        ).fetchall()
        self.assertEqual(rows, [{"roles_before": body["project_roles"], "roles_after": new_roles},
                                {"roles_before": new_roles, "roles_after": {}}])

    def test_deleted_account_loses_all_sessions_and_preserves_its_audit_history(self):
        body, other = self.multi_project_fixture()
        clients = [self.enterContext(TestClient(app)) for _ in range(2)]
        for client in clients:
            self.sign_in(self.login, body["password"], client)

        self.assertEqual(self.delete_account(1, self.actor).status_code, 403)
        self.assertEqual(self.set_roles(1, {}, self.actor).status_code, 403)
        revised = {self.project: "clerk", other: "reviewer"}
        self.assertEqual(self.set_roles(1, revised).status_code, 200)
        self.assertEqual(self.delete_account(1).status_code, 409)
        self.assertEqual(clients[0].get(API + "/me").status_code, 200)

        deleted = self.delete_account(2)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.delete_account(2).status_code, 200)
        for client in clients:
            self.assertEqual(client.get(API + "/me").status_code, 401)
        login = clients[0].post(API + "/login", json={"login": self.login, "password": body["password"]},
                                headers={"x-wb-login": "1"})
        self.assertEqual(login.status_code, 401)
        self.assertNotIn(self.login, [u["login"] for u in self.client.get(API + "/admin/users").json()["users"]])
        self.assertEqual(self.client.post(API + "/admin/users", json=body).status_code, 409)
        self.assertEqual(self.set_roles(3, {}).status_code, 404)

        for table in ("wb_sessions", "project_members"):
            self.assertIsNone(self.db.execute(
                f"SELECT 1 FROM secureagent.{table} WHERE login = %s", (self.login,)
            ).fetchone())
        stored = self.db.execute(
            """SELECT is_active, deleted_at, salt, digest FROM secureagent.wb_accounts
               WHERE login = %s""", (self.login,)
        ).fetchone()
        self.assertFalse(stored["is_active"])
        self.assertIsNotNone(stored["deleted_at"])
        self.assertEqual((stored["salt"], stored["digest"]), (b"", b""))
        events = self.db.execute(
            """SELECT action, request_id, roles_before, roles_after FROM secureagent.admin_events
               WHERE target_login = %s ORDER BY event_id""", (self.login,)
        ).fetchall()
        self.assertEqual([r["action"] for r in events], ["user_created", "memberships_changed", "user_deleted"])
        self.assertEqual(events[0]["roles_after"], body["project_roles"])
        self.assertEqual(events[-1], {"action": "user_deleted", "request_id": deleted.headers["x-request-id"],
                                     "roles_before": revised, "roles_after": {}})

    def project_action(self, action, revision):
        route = API + "/admin/projects/" + self.project
        params = {"expected_revision": revision}
        if action == "archive":
            return self.client.post(route + "/archive", params=params)
        return self.client.delete(route, params=params)

    def filing_upload(self, clerk, revision="R01"):
        raw = (ROOT / "examples/construction/QL-DOC-007_R01.md").read_bytes()
        return clerk.post(API + f"/{self.project}/upload", content=raw,
                          params={"document": "FILING", "revision": revision, "format": "md"})

    def test_archived_project_keeps_sources_and_search_but_freezes_document_writes(self):
        body, other = self.multi_project_fixture()
        clerk = self.enterContext(TestClient(app))
        self.sign_in(self.login, body["password"], clerk)
        reviewer = self.enterContext(TestClient(app))
        account = {**body, "login": self.login + "-r", "project_roles": {self.project: "reviewer"}}
        self.assertEqual(self.client.post(API + "/admin/users", json=account).status_code, 201)
        self.sign_in(account["login"], account["password"], reviewer)

        base = API + f"/{self.project}"
        current = base + "/FILING/R01"
        self.assertEqual(self.filing_upload(clerk).status_code, 200)
        sha = reviewer.get(current + "/preview").json()["sha256"]
        self.assertEqual(reviewer.post(current + "/approve", json={"sha256": sha, "expected_current": None}).status_code, 200)
        self.assertEqual(self.filing_upload(clerk, "R02").status_code, 200)
        source = clerk.get(current + "/original")
        self.assertEqual(source.status_code, 200)

        archived = self.project_action("archive", 1)
        self.assertEqual(archived.json(), {"project_id": self.project, "state": "archived", "lifecycle_revision": 2})
        self.assertEqual(self.project_action("archive", 1).json(), archived.json())
        self.assertEqual(self.project_action("delete", 1).status_code, 409)
        profile = clerk.get(API + "/me").json()
        self.assertEqual(profile["project_states"], {self.project: "archived", other: "active"})
        projects = self.client.get(API + "/admin/users").json()["projects"]
        self.assertEqual(next(p["state"] for p in projects if p["project_id"] == self.project), "archived")

        self.assertEqual(self.filing_upload(clerk, "R03").status_code, 409)
        draft = base + "/FILING/R02"
        refusal = reviewer.post(draft + "/approve", json={"sha256": sha, "expected_current": "R01"})
        self.assertEqual(refusal.status_code, 409)
        self.assertEqual(reviewer.get(draft + "/preview").json()["status"], "draft")
        self.assertEqual(self.set_roles(1, {self.project: "employee", other: "employee"}).status_code, 200)
        self.assertEqual(clerk.get(draft + "/preview").status_code, 404)
        self.assertEqual(clerk.get(base + "/versions").json()[0]["revision"], "R01")
        self.assertEqual(clerk.get(current + "/original").content, source.content)
        answer = clerk.post(base + "/ask", json={"question": "图纸会审纪要由谁归档，存到哪里？"})
        self.assertEqual(answer.status_code, 200)
        self.assertIn(self.project + "/FILING/R01/3.2", [row["chunk_id"] for row in answer.json()["results"]])
        audits = self.db.execute(
            """SELECT request_id FROM secureagent.admin_events
               WHERE project_id = %s AND action = 'project_archived'""", (self.project,),
        ).fetchall()
        self.assertEqual(audits, [{"request_id": archived.headers["x-request-id"]}])
        self.assertEqual(self.project_action("delete", 2).status_code, 200)

    def test_project_deletion_preserves_sources_and_revokes_only_this_project(self):
        body, other = self.multi_project_fixture()
        member = self.enterContext(TestClient(app))
        self.sign_in(self.login, body["password"], member)
        base = API + f"/{self.project}"
        self.assertEqual(self.filing_upload(member).status_code, 200)
        doc = base + "/FILING/R01"
        sha = member.get(doc + "/preview").json()["sha256"]
        raw = member.get(doc + "/original").content

        deleted = self.project_action("delete", 1)
        self.assertEqual(deleted.json(), {"project_id": self.project, "state": "deleted", "lifecycle_revision": 2})
        self.assertEqual(self.project_action("delete", 1).json(), deleted.json())
        self.assertEqual(self.project_action("archive", 2).status_code, 404)
        self.assertEqual(member.get(API + "/me").json()["project_roles"], {other: "employee"})
        self.assertEqual(member.get(API + f"/{other}/versions").status_code, 200)
        for route in (base + "/versions", doc + "/preview", doc + "/original"):
            self.assertEqual(member.get(route).status_code, 404)
        with patch.object(web, "run_query") as run:
            for endpoint in ("/ask", "/search"):
                self.assertEqual(member.post(base + endpoint, json={"question": "归档"}).status_code, 404)
            run.assert_not_called()
        directory = self.client.get(API + "/admin/users").json()
        self.assertNotIn(self.project, [p["project_id"] for p in directory["projects"]])
        account = next(u for u in directory["users"] if u["login"] == self.login)
        self.assertEqual((account["project_roles"], account["access_revision"]), ({other: "employee"}, 2))
        self.assertEqual(self.set_roles(1, {other: "reviewer"}).status_code, 409)
        self.assertEqual(self.set_roles(2, body["project_roles"]).status_code, 404)
        self.assertEqual(self.client.post(API + "/admin/projects",
                                         json={"project_id": self.project, "display_name": "重建"}).status_code, 409)

        self.assertEqual((control.ARCHIVE / (sha + ".md")).read_bytes(), raw)
        with closing(accounts.connect()) as registry:
            row = registry.execute("SELECT action FROM events WHERE project_id=? AND document_id='FILING'",
                                   (self.project,)).fetchone()
            self.assertEqual(row["action"], "received:draft")
        audit = self.db.execute(
            """SELECT request_id FROM secureagent.admin_events
               WHERE project_id = %s AND action = 'project_deleted'""", (self.project,),
        ).fetchall()
        self.assertEqual(audit, [{"request_id": deleted.headers["x-request-id"]}])

    def test_archive_during_upload_is_checked_before_registering_the_version(self):
        body, _ = self.multi_project_fixture()
        member = self.enterContext(TestClient(app))
        self.sign_in(self.login, body["password"], member)
        register = web.register_upload

        def after_body_received(key, data, extension, actor):
            admin.change_project_state(self.actor, self.project, "archived", 1, "during-upload")
            return register(key, data, extension, actor)

        with patch.object(web, "register_upload", side_effect=after_body_received):
            self.assertEqual(self.filing_upload(member).status_code, 409)
        with closing(accounts.connect()) as registry:
            self.assertIsNone(registry.execute(
                "SELECT 1 FROM versions WHERE project_id=? AND document_id='FILING'", (self.project,),
            ).fetchone())

    def test_finished_answer_is_withheld_if_project_was_deleted_during_search(self):
        body, _ = self.multi_project_fixture()
        member = self.enterContext(TestClient(app))
        self.sign_in(self.login, body["password"], member)

        def while_searching(*args, **kwargs):
            admin.change_project_state(self.actor, self.project, "deleted", 1, "during-search")
            return {"results": [{"text": "测试资料原文"}], "selected": None}

        with patch.object(web, "run_query", side_effect=while_searching):
            response = member.post(API + f"/{self.project}/ask", json={"question": "归档"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("测试资料原文", response.text)

    def test_postgres_documents_publish_search_and_original_with_no_local_register(self):
        body, other = self.multi_project_fixture()
        self.enterContext(patch.dict(os.environ, {"SECUREAGENT_DOCUMENT_BACKEND": "postgres"}))
        self.enterContext(patch.object(accounts, "connect", side_effect=AssertionError("unexpected SQLite access")))
        clerk = self.enterContext(TestClient(app))
        reviewer = self.enterContext(TestClient(app))
        self.sign_in(self.login, body["password"], clerk)
        account = {**body, "login": self.login + "-r", "project_roles": {self.project: "reviewer"}}
        self.assertEqual(self.client.post(API + "/admin/users", json=account).status_code, 201)
        self.sign_in(account["login"], account["password"], reviewer)
        base = API + "/" + self.project
        doc = base + "/FILING/"
        raw = (ROOT / "examples/construction/QL-DOC-007_R01.md").read_bytes()
        query = {"question": "图纸会审纪要由谁归档，存到哪里？"}
        self.assertTrue(self.filing_upload(clerk).json()["created"])
        self.assertFalse(self.filing_upload(clerk).json()["created"])
        self.assertEqual(clerk.post(base + "/ask", json=query).json()["results"], [])
        preview = reviewer.get(doc + "R01/preview").json()
        self.assertEqual((preview["status"], preview["current"]), ("draft", None))
        approval = {"sha256": preview["sha256"], "expected_current": None}
        self.assertEqual(reviewer.post(doc + "R01/approve", json=approval).status_code, 200)
        self.assertTrue(self.filing_upload(clerk, "R02").json()["created"])
        self.assertEqual(reviewer.post(doc + "R02/approve", json=approval).status_code, 409)
        approval["expected_current"] = "R01"
        self.assertEqual(reviewer.post(doc + "R02/approve", json=approval).status_code, 200)
        self.assertEqual(reviewer.post(doc + "R02/approve", json=approval).status_code, 200)
        events = reviewer.get(doc + "R02/preview").json()["events"]
        self.assertEqual([e["action"] for e in events], ["received:draft", "approved"])
        self.assertEqual(self.set_roles(1, {self.project: "employee", other: "employee"}).status_code, 200)
        self.assertEqual(clerk.get(base + "/versions").json(), [
            {"document_id": "FILING", "revision": "R02", "status": "effective", "title": preview["title"]},
        ])
        self.assertEqual(clerk.get(doc + "R01/original").status_code, 404)
        self.assertEqual(clerk.get(doc + "R02/original").content, raw)
        self.assertEqual(clerk.get(doc + "R02/preview").json()["events"], [])
        answer = clerk.post(base + "/ask", json=query).json()
        self.assertEqual(answer["results"][0]["chunk_id"], self.project + "/FILING/R02/3.2")
        self.assertEqual(clerk.post(API + "/" + other + "/ask", json=query).json()["results"], [])
        self.assertEqual(self.project_action("archive", 1).status_code, 200)
        self.assertEqual(reviewer.post(doc + "R02/approve", json=approval).status_code, 409)
        self.assertEqual(clerk.get(doc + "R02/original").content, raw)
        self.assertEqual(self.project_action("delete", 2).status_code, 200)
        self.assertEqual(clerk.get(doc + "R02/original").status_code, 404)
