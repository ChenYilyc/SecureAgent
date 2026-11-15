import argparse
import re
import secrets
from getpass import getpass

import psycopg

from app.workbench_accounts import password_digest


def main() -> None:
    parser = argparse.ArgumentParser(description="创建 SecureAgent 公司管理员")
    parser.add_argument("login", help="管理员用户名")
    login = parser.parse_args().login

    if not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", login):
        parser.error("用户名须为 1–48 个字符，可使用英文字母、数字、点、下划线和连字符。")

    db_url = getpass("本机 Supabase Database URL（隐藏输入）：").strip()
    if not db_url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("请输入本地 Supabase 显示的 PostgreSQL 连接地址。")

    password = getpass("新管理员密码（12–256 个字符）：")
    if not 12 <= len(password) <= 256:
        raise SystemExit("密码长度须为 12–256 个字符。")
    if password != getpass("再次输入管理员密码："):
        raise SystemExit("两次输入的密码不同。")

    salt = secrets.token_bytes(16)
    digest = password_digest(password, salt)

    try:
        with psycopg.connect(db_url, connect_timeout=10) as db:
            created = db.execute(
                """
                INSERT INTO secureagent.wb_accounts
                    (login, salt, digest, is_company_admin)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (login) DO NOTHING
                RETURNING login
                """,
                (login, salt, digest),
            ).fetchone()
    except psycopg.Error as exc:
        raise SystemExit(
            f"PostgreSQL 操作失败：{type(exc).__name__}，"
            f"SQLSTATE={exc.sqlstate or '无'}"
        ) from None

    if created is None:
        raise SystemExit(f"用户名 {login} 已存在，本次创建了 0 个账号。")

    print(f"已创建公司管理员 {login}")


if __name__ == "__main__":
    main()