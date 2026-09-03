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
# 作品ページからいいね・閲覧数を取得
# ============================================================

def get_number_from_icon(page, icon_name):
    """
    #eyes / #heart のアイコンを探して、
    同じGroup内に表示されている数字を取得する
    """

    try:
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

    except Exception as e:
        print(f"アイコン取得エラー ({icon_name}): {e}")
        return None


def get_program_data(page, url):
    """
    1作品分の閲覧数・いいね数を取得
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
# 前回のユーザー情報を取得
# ============================================================

def get_users_old_data(conn):
    """
    各ユーザーについて、

        id
        program_urls
        program_views
        program_likes

    を取得する。

    URL → ユーザー情報

    の形にする。
    """

    result = {}

    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                id,
                program_urls,
                program_views,
                program_likes
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        rows = cur.fetchall()

    for user_id, program_urls, program_views, program_likes in rows:

        program_urls = program_urls or []
        program_views = program_views or []
        program_likes = program_likes or []

        for i, url in enumerate(program_urls):

            if not url:
                continue

            old_views = 0
            old_likes = 0

            if i < len(program_views):
                old_views = program_views[i] or 0

            if i < len(program_likes):
                old_likes = program_likes[i] or 0

            result[url.strip()] = {
                "user_id": user_id,
                "old_views": old_views,
                "old_likes": old_likes,
            }

    return result


# ============================================================
# 通知を作成
# ============================================================

def create_like_notification(
    conn,
    user_id,
    title,
    old_likes,
    new_likes
):
    """
    いいねが増えていた場合に通知を作成する
    """

    difference = new_likes - old_likes

    if difference <= 0:
        return False

    message = f"「{title}」にいいねがされました"

    with conn.cursor() as cur:

        # ----------------------------------------------------
        # 同じ通知の連続作成を防止
        #
        # 直前に同じユーザー・同じメッセージが作られていたら
        # 今回は作らない
        # ----------------------------------------------------

        cur.execute("""
            SELECT id
            FROM public.notifications
            WHERE user_id = %s
              AND type = 'like'
              AND message = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (
            user_id,
            message,
        ))

        existing = cur.fetchone()

        if existing:
            print(
                f"  通知は既に存在: {message}"
            )
            return False

        # ----------------------------------------------------
        # 通知作成
        # ----------------------------------------------------

        cur.execute("""
            INSERT INTO public.notifications (
                user_id,
                actor_id,
                type,
                post_id,
                created_at,
                is_read,
                message
            )
            VALUES (
                %s,
                NULL,
                'like',
                NULL,
                NOW(),
                FALSE,
                %s
            )
        """, (
            user_id,
            message,
        ))

    print(
        f"  ★ 通知作成: {message}"
    )

    return True


# ============================================================
# ユーザーの program_views / program_likes を更新
# ============================================================

def update_users(
    conn,
    stats,
    old_data
):
    """
    users.program_urls の順番に合わせて

        program_views
        program_likes

    を更新する。

    同時に、いいねが増えた作品について
    notifications を作成する。
    """

    updated_users = 0
    notification_count = 0

    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                id,
                program_urls
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        rows = cur.fetchall()

        for user_id, program_urls in rows:

            program_urls = program_urls or []

            views = []
            likes = []

            for url in program_urls:

                url = url.strip()

                data = stats.get(url)

                # --------------------------------------------
                # 取得できなかった
                # --------------------------------------------

                if data is None:

                    views.append(0)
                    likes.append(0)

                    continue

                # --------------------------------------------
                # 今回取得した値
                # --------------------------------------------

                new_views = (
                    data["views"]
                    if data["views"] is not None
                    else 0
                )

                new_likes = (
                    data["likes"]
                    if data["likes"] is not None
                    else 0
                )

                views.append(new_views)
                likes.append(new_likes)

                # --------------------------------------------
                # 前回の値
                # --------------------------------------------

                previous = old_data.get(url)

                if previous is None:

                    # 初回取得の場合
                    # いきなり通知を出さない
                    print(
                        f"  初回登録: {url}"
                    )

                    continue

                old_likes = previous["old_likes"]

                # --------------------------------------------
                # いいねが増えた
                # --------------------------------------------

                if new_likes > old_likes:

                    # ----------------------------------------
                    # タイトル取得
                    #
                    # statsにtitleがあれば使用
                    # なければURLをタイトル代わりにする
                    # ----------------------------------------

                    title = data.get("title")

                    if not title:
                        title = url

                    print()
                    print(
                        "★ いいね増加検出"
                    )
                    print(
                        f"  ユーザーID: {user_id}"
                    )
                    print(
                        f"  作品: {title}"
                    )
                    print(
                        f"  前回: {old_likes}"
                    )
                    print(
                        f"  今回: {new_likes}"
                    )
                    print(
                        f"  増加: +{new_likes - old_likes}"
                    )

                    created = create_like_notification(
                        conn=conn,
                        user_id=user_id,
                        title=title,
                        old_likes=old_likes,
                        new_likes=new_likes,
                    )

                    if created:
                        notification_count += 1

            # -----------------------------------------------
            # users更新
            # -----------------------------------------------

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

    return updated_users, notification_count


# ============================================================
# メイン
# ============================================================

def main():

    fetched_at = datetime.now(timezone.utc).isoformat()

    print("=" * 60)
    print("プログラム統計・いいね通知更新開始")
    print("=" * 60)

    # ========================================================
    # DB
    # ========================================================

    with psycopg.connect(DATABASE_URL) as conn:

        # ----------------------------------------------------
        # まず「前回のいいね数」を保存
        # ----------------------------------------------------

        print()
        print("前回データを取得しています...")

        old_data = get_users_old_data(conn)

        print(
            f"前回データ取得数: {len(old_data)}作品"
        )

        # ----------------------------------------------------
        # URL一覧取得
        # ----------------------------------------------------

        programs = get_all_programs(conn)

        print()
        print(
            f"取得対象プログラム数: {len(programs)}"
        )

        for url in programs:
            print(f"  {url}")

        # ====================================================
        # Playwright
        # ====================================================

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

                # ------------------------------------------------
                # タイトルも取得できるようにする
                # ------------------------------------------------

                if result["status"] == "success":

                    try:

                        # ページタイトル
                        page_title = page.locator(
                            "h1"
                        ).first.inner_text(
                            timeout=3000
                        ).strip()

                        if page_title:
                            result["title"] = page_title

                    except Exception:

                        result["title"] = None

                stats[url] = result

                if result["status"] == "success":

                    print(
                        f"  閲覧数: {result['views']}"
                    )

                    print(
                        f"  いいね: {result['likes']}"
                    )

                    if result.get("title"):
                        print(
                            f"  タイトル: {result['title']}"
                        )

            browser.close()

        # ====================================================
        # DB更新 + 通知
        # ====================================================

        updated_users, notification_count = update_users(
            conn,
            stats,
            old_data
        )

    # ========================================================
    # 結果JSON
    # ========================================================

    output = {
        "fetched_at": fetched_at,
        "program_count": len(programs),
        "updated_users": updated_users,
        "notification_count": notification_count,
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

    # ========================================================
    # 結果表示
    # ========================================================

    print()
    print("=" * 60)
    print("更新完了")
    print("=" * 60)

    print(
        f"プログラム数: {len(programs)}"
    )

    print(
        f"ユーザー更新数: {updated_users}"
    )

    print(
        f"作成した通知数: {notification_count}"
    )

    print(
        "result.json を保存しました"
    )


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":
    main()
