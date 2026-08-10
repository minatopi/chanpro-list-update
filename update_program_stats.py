import os
import re
import json
from datetime import datetime, timezone

import psycopg
from playwright.sync_api import sync_playwright


# ============================================================
# PostgreSQL
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]


# ============================================================
# URLからプログラムデータを取得
# ============================================================

def get_number_from_icon(page, icon_name):
    """
    #eyes / #heart のアイコンを探して、
    同じGroup内に表示されている数字を取得する
    """

    icon = page.locator(f'use[href*="#{icon_name}"]')

    if icon.count() == 0:
        return None

    group = icon.first.locator(
        "xpath=ancestor::div[contains(@class, 'Group')][1]"
    )

    if group.count() == 0:
        return None

    text = group.inner_text()

    numbers = re.findall(r"\d[\d,]*", text)

    if not numbers:
        return None

    return int(numbers[-1].replace(",", ""))


def get_program_data(page, url):
    """
    1ページ分の閲覧数・いいね数を取得
    """

    print(f"取得中: {url}")

    try:
        page.goto(
            url,
            wait_until="networkidle",
            timeout=60000
        )

        # BubbleのJavaScript処理を待つ
        page.wait_for_timeout(3000)

        views = get_number_from_icon(page, "eyes")
        likes = get_number_from_icon(page, "heart")

        return {
            "url": url,
            "views": views,
            "likes": likes,
            "status": "success",
        }

    except Exception as e:

        print(f"取得失敗: {url}")
        print(str(e))

        return {
            "url": url,
            "views": None,
            "likes": None,
            "status": "error",
            "error": str(e),
        }


# ============================================================
# DBから全プログラムURLを取得
# ============================================================

def get_all_programs(conn):
    """
    users.program_urls を全ユーザーから取得し、
    重複URLを除外して一覧化する
    """

    programs = set()

    with conn.cursor() as cur:

        cur.execute("""
            SELECT program_urls
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        rows = cur.fetchall()

    for row in rows:

        urls = row[0] or []

        for url in urls:

            if url:
                programs.add(url.strip())

    return sorted(programs)


# ============================================================
# 全ユーザーのプログラム情報を更新
# ============================================================

def update_users(conn, stats):
    """
    stats:
        {
            "URL": {
                "views": 123,
                "likes": 45
            }
        }

    users.program_urls の順番に合わせて
    program_views / program_likes を作る
    """

    with conn.cursor() as cur:

        cur.execute("""
            SELECT id, program_urls
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        rows = cur.fetchall()

        updated_users = 0

        for user_id, program_urls in rows:

            program_urls = program_urls or []

            views = []
            likes = []

            for url in program_urls:

                data = stats.get(url)

                if data is None:
                    # 取得できなかったURL
                    # 既存値を維持したい場合は後述の方式に変更可能
                    views.append(0)
                    likes.append(0)

                else:
                    views.append(
                        data["views"]
                        if data["views"] is not None
                        else 0
                    )

                    likes.append(
                        data["likes"]
                        if data["likes"] is not None
                        else 0
                    )

            cur.execute("""
                UPDATE public.users
                SET
                    program_views = %s,
                    program_likes = %s
                WHERE id = %s
            """, (
                views,
                likes,
                user_id,
            ))

            updated_users += 1

    conn.commit()

    return updated_users


# ============================================================
# メイン
# ============================================================

def main():

    fetched_at = datetime.now(timezone.utc).isoformat()

    print("=" * 60)
    print("プログラム統計更新開始")
    print("=" * 60)

    # --------------------------------------------------------
    # DB接続
    # --------------------------------------------------------

    with psycopg.connect(DATABASE_URL) as conn:

        programs = get_all_programs(conn)

        print()
        print(f"取得対象プログラム数: {len(programs)}")

        for url in programs:
            print(f"  {url}")

        # ----------------------------------------------------
        # Playwright
        # ----------------------------------------------------

        stats = {}

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            page = browser.new_page(
                viewport={
                    "width": 1280,
                    "height": 2000
                },
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            for url in programs:

                result = get_program_data(
                    page,
                    url
                )

                stats[url] = result

                if result["status"] == "success":

                    print(
                        f"  閲覧数: {result['views']}"
                    )

                    print(
                        f"  いいね: {result['likes']}"
                    )

            browser.close()

        # ----------------------------------------------------
        # DB更新
        # ----------------------------------------------------

        updated_users = update_users(
            conn,
            stats
        )

    # --------------------------------------------------------
    # 結果
    # --------------------------------------------------------

    output = {
        "fetched_at": fetched_at,
        "program_count": len(programs),
        "updated_users": updated_users,
        "programs": stats,
    }

    with open(
        "result.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2
        )

    print()
    print("=" * 60)
    print("更新完了")
    print("=" * 60)
    print(f"プログラム数: {len(programs)}")
    print(f"ユーザー更新数: {updated_users}")


if __name__ == "__main__":
    main()
