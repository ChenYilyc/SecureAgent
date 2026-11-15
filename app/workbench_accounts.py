"""Provision local users. Roles and project grants are assigned at the console."""
import argparse
from contextlib import closing
from getpass import getpass
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
import time

from app import document_control as control

ROLES = {"clerk": "资料员", "reviewer": "审批人", "employee": "员工"}
HOURS = 8
AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS wb_accounts (
 login TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('clerk','reviewer','employee')), projects TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wb_sessions (
 token_hash TEXT PRIMARY KEY, login TEXT NOT NULL, csrf TEXT NOT NULL, expires INTEGER NOT NULL
);
"""


def connect():
    control.REGISTER.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(control.REGISTER, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript(control.SCHEMA + AUTH_SCHEMA)
    return db


def password_digest(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64*1024*1024)


def add(db, login, password, role, projects):
    if (not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", login) or role not in ROLES
            or not projects or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p) for p in projects)):
        raise ValueError("账号、角色或项目编号格式无效。")
    if not 12 <= len(password) <= 256:
        raise ValueError("口令长度需为 12 至 256 个字符。")
    salt = secrets.token_bytes(16)
    db.execute("INSERT INTO wb_accounts VALUES (?,?,?,?,?)",
               (login, salt, password_digest(password, salt), role, json.dumps(sorted(set(projects)))))


def sign_in(db, login, password):
    row = db.execute("SELECT * FROM wb_accounts WHERE login=?", (login,)).fetchone()
    calculated = password_digest(password, row["salt"] if row else bytes(16))
    if not row or not hmac.compare_digest(row["digest"], calculated):
        return None
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    with db:
        db.execute("DELETE FROM wb_sessions WHERE expires <= ?", (int(time.time()),))
        db.execute("INSERT INTO wb_sessions VALUES (?,?,?,?)",
                   (fingerprint(token), login, csrf, int(time.time()) + HOURS*3600))
    return token


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session(db, token):
    if not token or len(token) > 100:
        return None
    row = db.execute("""SELECT a.login,a.role,a.projects,s.csrf FROM wb_sessions s
                     JOIN wb_accounts a USING(login) WHERE s.token_hash=? AND s.expires>?""",
                     (fingerprint(token), int(time.time()))).fetchone()
    return {**dict(row), "projects": json.loads(row["projects"])} if row else None


def main():
    parser = argparse.ArgumentParser(description="本机管理员登记工作台账号")
    sub = parser.add_subparsers(dest="cmd", required=True)
    demo = sub.add_parser("demo", help="为一个项目建立三个演示身份，仅在创建时显示随机口令")
    demo.add_argument("--project", default="QL01")
    create = sub.add_parser("add", help="为指定人员和项目创建账号")
    create.add_argument("login")
    create.add_argument("--role", choices=ROLES, required=True)
    create.add_argument("--project", action="append", required=True)
    args = parser.parse_args()
    try:
        with closing(connect()) as db:
            if args.cmd == "demo":
                credentials = []
                with db:
                    for role in ROLES:
                        login = f"demo-{role}"
                        if db.execute("SELECT 1 FROM wb_accounts WHERE login=?", (login,)).fetchone():
                            continue
                        password = secrets.token_urlsafe(15)
                        add(db, login, password, role, [args.project])
                        credentials.append((ROLES[role], login, password))
                for name, login, password in credentials:
                    print(f"{name}  {login}  {password}")
                if not credentials:
                    print("演示账号已存在；保留原有口令和项目授权。")
            else:
                password = getpass("新口令：")
                if password != getpass("再次输入："):
                    sys.exit("两次输入不同。")
                with db:
                    add(db, args.login, password, args.role, args.project)
                print("账号已登记。")
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"创建账号失败：{exc}\n")


if __name__ == "__main__":
    main()
