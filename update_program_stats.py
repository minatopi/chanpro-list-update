
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
# 設定
# ============================================================

PAGE_WAIT_MS = 3000
PAGE_TIMEOUT = 60000


# ============================================================
# アイコンから数字を取得
# ============================================================

def get_number_from_icon(page, icon_name):
    """
    #eyes / #heart のアイコンを探して、
    同じGroup内に表示されている数字を取得する
    """

    try:
        icon = page.locator(
            f'use[href*="#{icon_name}"]'
        )

        if icon.count() == 0:
            return None

        group = icon.first.locator(
            "xpath=ancestor::div[contains(@class, 'Group')][1]"
        )

        if group.count() == 0:
            return None

        text = group.inner_text()

        numbers = re.findall(
            r"\d[\d,]*",
            text
        )

        if not numbers:
            return None

        return int(
            numbers[-1].replace(",", "")
        )

    except Exception as e:

        print(
            f"アイコン取得エラー [{icon_name}]: {e}"
        )

        return None


# ============================================================
# プログラム1件取得
# ============================================================

def get_program_data(page, url):

    print()
    print("-" * 60)
    print(f"取得中: {url}")

    try:

        page.goto(
            url,
            wait_until="networkidle",
            timeout=PAGE_TIMEOUT
        )

        # BubbleのJavaScript処理を待つ
        page.wait_for_timeout(
            PAGE_WAIT_MS
        )

        views = get_number_from_icon(
            page,
            "eyes"
        )

        likes = get_number_from_icon(
            page,
            "heart"
        )

        # 両方取れなかった場合は失敗扱い
        if views is None and likes is None:

            print("  データを取得できませんでした")

            return {
                "url": url,
                "views": None,
                "likes": None,
                "status": "error",
                "error": "views / likes が取得できませんでした"
            }

        print(
            f"  閲覧数: {views}"
        )

        print(
            f"  いいね: {likes}"
        )

        return {
            "url": url,
            "views": views,
            "likes": likes,
            "status": "success",
        }

    except Exception as e:

        print()
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
# DBから全ユーザーのプログラム情報を取得
# ============================================================

def get_users_data(conn):

    """
    更新前の情報を取得する。

    重要:
    この時点の program_likes が「前回のいいね数」になる。
    """

    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                id,
                program_urls,
                program_likes,
                program_views
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        return cur.fetchall()


# ============================================================
# 全プログラムURLを取得
# ============================================================

def get_all_programs(users_data):

    programs = set()

    for row in users_data:

        program_urls = row[1] or []

        for url in program_urls:

            if not url:
                continue

            url = str(url).strip()

            if url:
                programs.add(url)

    return sorted(programs)


# ============================================================
# いいね増加通知
# ============================================================

def create_like_notifications(
    conn,
    users_data,
    stats
):
    """
    前回の program_likes と今回の取得値を比較する。

    増えていた場合だけ notifications にINSERTする。

    既存notificationsテーブルを変更しないため、
    typeには

        program_like|URL|増加数

    の情報を入れる。

    例:

        program_like|https://chanpro.jp/...|3

    """

    notification_count = 0

    with conn.cursor() as cur:

        for row in users_data:

            user_id = row[0]
            program_urls = row[1] or []
            old_likes = row[2] or []

            # ------------------------------------------------
            # ユーザーの全プログラムを確認
            # ------------------------------------------------

            for index, url in enumerate(program_urls):

                if not url:
                    continue

                url = str(url).strip()

                if not url:
                    continue

                # ------------------------------------------------
                # 前回のいいね数
                # ------------------------------------------------

                if index < len(old_likes):

                    old_value = old_likes[index]

                    try:
                        previous_likes = int(
                            old_value or 0
                        )

                    except Exception:
                        previous_likes = 0

                else:

                    # 初回登録など
                    previous_likes = 0

                # ------------------------------------------------
                # 今回の取得結果
                # ------------------------------------------------

                data = stats.get(url)

                if not data:
                    continue

                # 取得失敗なら通知しない
                if data.get("status") != "success":
                    continue

                current_likes = data.get("likes")

                if current_likes is None:
                    continue

                try:
                    current_likes = int(
                        current_likes
                    )

                except Exception:
                    continue

                # ------------------------------------------------
                # 増加数
                # ------------------------------------------------

                difference = (
                    current_likes
                    - previous_likes
                )

                # 増えていなければ何もしない
                if difference <= 0:
                    continue

                # ------------------------------------------------
                # ログ
                # ------------------------------------------------

                print()
                print("★ いいね増加を検出")
                print(
                    f"  ユーザーID: {user_id}"
                )
                print(
                    f"  プロジェクト: {url}"
                )
                print(
                    f"  前回: {previous_likes}"
                )
                print(
                    f"  今回: {current_likes}"
                )
                print(
                    f"  増加: +{difference}"
                )

                # ------------------------------------------------
                # 通知タイプ
                #
                # 既存テーブルを変更しないため、
                # URLと増加数をtypeに格納
                # ------------------------------------------------

                notification_type = (
                    "program_like"
                    f"|{url}"
                    f"|{difference}"
                )

                # ------------------------------------------------
                # 同じ通知の重複チェック
                #
                # 同じ実行を何度も処理しても、
                # 同じ内容の通知を大量に作らない。
                #
                # created_atは完全一致ではなく、
                # 直近10分を確認する。
                # ------------------------------------------------

                cur.execute("""
                    SELECT id
                    FROM public.notifications
                    WHERE user_id = %s
                      AND type = %s
                      AND created_at >= now() - interval '10 minutes'
                    LIMIT 1
                """, (
                    user_id,
                    notification_type,
                ))

                exists = cur.fetchone()

                if exists:

                    print(
                        "  → 同じ通知が既に存在するためスキップ"
                    )

                    continue

                # ------------------------------------------------
                # 通知INSERT
                # ------------------------------------------------

                cur.execute("""
                    INSERT INTO public.notifications (
                        user_id,
                        actor_id,
                        type,
                        post_id,
                        created_at,
                        message_id,
                        is_read
                    )
                    VALUES (
                        %s,
                        NULL,
                        %s,
                        NULL,
                        now(),
                        NULL,
                        false
                    )
                """, (
                    user_id,
                    notification_type,
                ))

                notification_count += 1

                print(
                    "  → 通知を作成しました"
                )

    return notification_count


# ============================================================
# users.program_views / program_likes 更新
# ============================================================

def update_users(
    conn,
    users_data,
    stats
):

    updated_users = 0

    with conn.cursor() as cur:

        for row in users_data:

            user_id = row[0]
            program_urls = row[1] or []
            old_likes = row[2] or []
            old_views = row[3] or []

            new_likes = []
            new_views = []

            # ------------------------------------------------
            # URLの順番を維持して更新
            # ------------------------------------------------

            for index, url in enumerate(program_urls):

                url = str(url).strip()

                data = stats.get(url)

                # =================================================
                # 取得成功
                # =================================================

                if (
                    data
                    and data.get("status") == "success"
                ):

                    # ---------------------------------------------
                    # views
                    # ---------------------------------------------

                    if data.get("views") is not None:

                        new_views.append(
                            int(data["views"])
                        )

                    elif index < len(old_views):

                        # 取得できなかった場合は旧値維持
                        new_views.append(
                            old_views[index]
                        )

                    else:

                        new_views.append(0)

                    # ---------------------------------------------
                    # likes
                    # ---------------------------------------------

                    if data.get("likes") is not None:

                        new_likes.append(
                            int(data["likes"])
                        )

                    elif index < len(old_likes):

                        # 取得できなかった場合は旧値維持
                        new_likes.append(
                            old_likes[index]
                        )

                    else:

                        new_likes.append(0)

                # =================================================
                # 取得失敗
                # =================================================

                else:

                    # ---------------------------------------------
                    # views
                    # ---------------------------------------------

                    if index < len(old_views):

                        new_views.append(
                            old_views[index]
                        )

                    else:

                        new_views.append(0)

                    # ---------------------------------------------
                    # likes
                    # ---------------------------------------------

                    if index < len(old_likes):

                        new_likes.append(
                            old_likes[index]
                        )

                    else:

                        new_likes.append(0)

            # ------------------------------------------------
            # DB更新
            # ------------------------------------------------

            cur.execute("""
                UPDATE public.users
                SET
                    program_views = %s,
                    program_likes = %s
                WHERE id = %s
            """, (
                new_views,
                new_likes,
                user_id,
            ))

            updated_users += 1

    return updated_users


# ============================================================
# 結果JSON保存
# ============================================================

def save_result(
    fetched_at,
    programs,
    stats,
    updated_users,
    notification_count
):

    output = {

        "fetched_at": fetched_at,

        "program_count": len(programs),

        "updated_users": updated_users,

        "notifications_created":
            notification_count,

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

    return output


# ============================================================
# メイン
# ============================================================

def main():

    fetched_at = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    print("=" * 60)
    print("プログラム統計更新開始")
    print("=" * 60)

    # ========================================================
    # PostgreSQL
    # ========================================================

    with psycopg.connect(
        DATABASE_URL
    ) as conn:

        # ----------------------------------------------------
        # 1.
        # 更新前のusers情報を取得
        # ----------------------------------------------------

        print()
        print("ユーザーデータを取得中...")

        users_data = get_users_data(
            conn
        )

        print(
            f"ユーザー数: {len(users_data)}"
        )

        # ----------------------------------------------------
        # 2.
        # 全プログラムURL
        # ----------------------------------------------------

        programs = get_all_programs(
            users_data
        )

        print()
        print(
            f"取得対象プログラム数: {len(programs)}"
        )

        for url in programs:

            print(
                f"  {url}"
            )

        # ====================================================
        # Playwright
        # ====================================================

        stats = {}

        print()
        print("=" * 60)
        print("Playwright開始")
        print("=" * 60)

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
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/120.0.0.0 "
                    "Safari/537.36"
                ),
            )

            # ------------------------------------------------
            # 全URL取得
            # ------------------------------------------------

            for url in programs:

                result = get_program_data(
                    page,
                    url
                )

                stats[url] = result

            browser.close()

        # ====================================================
        # 通知作成
        # ====================================================

        print()
        print("=" * 60)
        print("いいね増加チェック")
        print("=" * 60)

        notification_count = (
            create_like_notifications(
                conn,
                users_data,
                stats
            )
        )

        # ====================================================
        # users更新
        # ====================================================

        print()
        print("=" * 60)
        print("ユーザー情報更新")
        print("=" * 60)

        updated_users = update_users(
            conn,
            users_data,
            stats
        )

        # ----------------------------------------------------
        # 全処理成功
        # ----------------------------------------------------

        conn.commit()

    # ========================================================
    # JSON
    # ========================================================

    save_result(
        fetched_at,
        programs,
        stats,
        updated_users,
        notification_count
    )

    # ========================================================
    # 結果
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
        f"通知作成数: {notification_count}"
    )

    print()
    print(
        "result.json を保存しました"
    )


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print()
        print("=" * 60)
        print("ERROR")
        print("=" * 60)
        print(str(e))

        raise

