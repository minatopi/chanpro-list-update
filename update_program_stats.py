import os
import re
import json
from datetime import datetime, timezone

import psycopg
from playwright.sync_api import sync_playwright


# ============================================================
# 設定
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

STATE_FILE = "program_likes.json"


# ============================================================
# JSON読み込み
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):

        print("program_likes.json がありません")
        print("初回実行として開始します")

        return {
            "updated_at": None,
            "users": {}
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as e:

        print(
            "JSON読み込みエラー:",
            e
        )

        return {
            "updated_at": None,
            "users": {}
        }


# ============================================================
# JSON保存
# ============================================================

def save_state(data):

    data["updated_at"] = (
        datetime.now(timezone.utc)
        .isoformat()
    )

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# カード解析
# ============================================================

def parse_card(text):

    lines = [
        l.strip()
        for l in text.split("\n")
        if l.strip()
    ]

    # 不要な文字を除外
    lines = [
        l
        for l in lines
        if l not in ["ログイン"]
        and not l.startswith("Lv.")
    ]

    if not lines:
        return None

    # --------------------------------------------------------
    # 最初の文字列を作品タイトル
    # --------------------------------------------------------

    title = lines[0]

    # --------------------------------------------------------
    # 数字取得
    # --------------------------------------------------------

    nums = re.findall(
        r"\d[\d,]*",
        text
    )

    likes = 0
    views = 0

    if len(nums) >= 1:

        likes = int(
            nums[0].replace(",", "")
        )

    if len(nums) >= 2:

        views = int(
            nums[1].replace(",", "")
        )

    return {
        "title": title,
        "likes": likes,
        "views": views
    }


# ============================================================
# プロフィールページから作品取得
# ============================================================

def scrape_profile(page, profile_url):

    print()
    print("=" * 60)
    print("プロフィール取得")
    print(profile_url)
    print("=" * 60)

    try:

        page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        # Bubbleの描画待ち
        page.wait_for_timeout(8000)

        # ----------------------------------------------------
        # 作品カードの入っているコンテナ
        # ----------------------------------------------------

        container = page.locator(
            "div.bubble-element.Group.baTcwaH1"
        ).first

        container.wait_for(
            timeout=30000
        )

        cards = container.locator(
            "div.clickable-element"
        ).all()

        print(
            "cards:",
            len(cards)
        )

        results = []

        for i, card in enumerate(cards):

            try:

                text = card.inner_text()

                print()
                print(
                    f"--- CARD {i + 1} ---"
                )

                print(text)

                parsed = parse_card(
                    text
                )

                if parsed:

                    print(
                        f"タイトル: {parsed['title']}"
                    )

                    print(
                        f"いいね: {parsed['likes']}"
                    )

                    print(
                        f"閲覧数: {parsed['views']}"
                    )

                    results.append(
                        parsed
                    )

            except Exception as e:

                print(
                    f"CARD {i + 1} error:",
                    e
                )

        return results

    except Exception as e:

        print(
            "プロフィール取得失敗:",
            profile_url
        )

        print(
            str(e)
        )

        return []


# ============================================================
# DBからユーザーとプロフィールURLを取得
# ============================================================

def get_users(conn):

    """
    usersテーブルから

        id
        program_urls

    を取得。

    program_urlsからプロフィールURLを作る。

    ※プロフィールURLが別カラムにある場合は
      ここを変更する。
    """

    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                id,
                program_urls
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        rows = cur.fetchall()

    return rows


# ============================================================
# program_urlsからプロフィールURLを取得
# ============================================================

def get_profile_url(program_urls):

    """
    現在のprogram_urlsが作品URLの配列の場合、
    ここでプロフィールURLを決める。

    """

    if not program_urls:
        return None

    # --------------------------------------------------------
    # ここは実際のサイト構造に合わせる必要があります。
    #
    # もし program_urls 自体がプロフィールURLではなく
    # 「作品URL」の配列なら、
    # DBにプロフィールURLを保存しているカラムが
    # 別にある場合はそちらを使うのが確実です。
    # --------------------------------------------------------

    return None


# ============================================================
# 通知作成
# ============================================================

def create_notification(
    conn,
    user_id,
    title
):

    message = (
        f"「{title}」にいいねがされました"
    )

    try:

        with conn.cursor() as cur:

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
                message
            ))

        print()
        print(
            "★ 通知作成成功"
        )

        print(
            f"  user_id: {user_id}"
        )

        print(
            f"  message: {message}"
        )

        return True

    except Exception as e:

        print()
        print(
            "★★ 通知作成失敗 ★★"
        )

        print(
            f"  user_id: {user_id}"
        )

        print(
            f"  message: {message}"
        )

        print(
            f"  error: {e}"
        )

        return False


# ============================================================
# メイン
# ============================================================

def main():

    print("=" * 70)
    print("作品別いいね数チェック")
    print("=" * 70)

    # --------------------------------------------------------
    # 前回JSON
    # --------------------------------------------------------

    old_state = load_state()

    old_users = old_state.get(
        "users",
        {}
    )

    # --------------------------------------------------------
    # 新しいJSON
    # --------------------------------------------------------

    new_state = {
        "updated_at": None,
        "users": {}
    }

    notification_count = 0

    # ========================================================
    # DB
    # ========================================================

    with psycopg.connect(
        DATABASE_URL
    ) as conn:

        users = get_users(
            conn
        )

        print()
        print(
            f"ユーザー数: {len(users)}"
        )

        # ====================================================
        # Playwright
        # ====================================================

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
                )
            )

            # =================================================
            # ユーザーごと
            # =================================================

            for user_id, program_urls in users:

                user_id = str(
                    user_id
                )

                print()
                print("#" * 70)
                print(
                    "USER:",
                    user_id
                )
                print("#" * 70)

                # ------------------------------------------------
                # プロフィールURL
                # ------------------------------------------------
                #
                # ここが重要
                #
                # 実際に「そのユーザーのプロフィールURL」が
                # DBのどこに入っているかによって変更します。
                #
                # 例:
                #
                # profile_url = user_profile_url
                #
                # ------------------------------------------------

                profile_url = get_profile_url(
                    program_urls
                )

                if not profile_url:

                    print(
                        "プロフィールURLを取得できません"
                    )

                    continue

                # ------------------------------------------------
                # プロフィールから全作品取得
                # ------------------------------------------------

                posts = scrape_profile(
                    page,
                    profile_url
                )

                # ------------------------------------------------
                # 新しいJSONへ保存
                # ------------------------------------------------

                new_state["users"][
                    user_id
                ] = {
                    "profile_url": profile_url,
                    "programs": {}
                }

                # ------------------------------------------------
                # 前回ユーザーデータ
                # ------------------------------------------------

                old_user = old_users.get(
                    user_id,
                    {}
                )

                old_programs = old_user.get(
                    "programs",
                    {}
                )

                # ------------------------------------------------
                # 作品ごと
                # ------------------------------------------------

                for post in posts:

                    title = post["title"]

                    likes = post["likes"]

                    # 作品をタイトルで保存
                    #
                    # 同名作品がある場合はURL方式に
                    # 変更した方が安全。
                    #

                    key = title

                    # ------------------------------------------------
                    # 新JSON
                    # ------------------------------------------------

                    new_state["users"][
                        user_id
                    ]["programs"][
                        key
                    ] = {
                        "title": title,
                        "likes": likes,
                        "views": post["views"]
                    }

                    # ------------------------------------------------
                    # 前回値
                    # ------------------------------------------------

                    old_post = old_programs.get(
                        key
                    )

                    # ------------------------------------------------
                    # 初回
                    # ------------------------------------------------

                    if old_post is None:

                        print()
                        print(
                            "初回登録:"
                        )

                        print(
                            f"  作品: {title}"
                        )

                        print(
                            f"  いいね: {likes}"
                        )

                        continue

                    old_likes = old_post.get(
                        "likes",
                        0
                    )

                    # ------------------------------------------------
                    # 増加
                    # ------------------------------------------------

                    if likes > old_likes:

                        increase = (
                            likes
                            - old_likes
                        )

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
                            f"  今回: {likes}"
                        )

                        print(
                            f"  増加: +{increase}"
                        )

                        # ------------------------------------------------
                        # 通知
                        # ------------------------------------------------

                        if create_notification(
                            conn,
                            user_id,
                            title
                        ):

                            notification_count += 1

            browser.close()

        # ========================================================
        # DB確定
        # ========================================================

        conn.commit()

        print()
        print(
            "DB commit完了"
        )

    # ========================================================
    # JSON保存
    # ========================================================

    save_state(
        new_state
    )

    # ========================================================
    # 完了
    # ========================================================

    print()
    print("=" * 70)
    print("完了")
    print("=" * 70)

    print(
        f"ユーザー数: {len(users)}"
    )

    print(
        f"通知作成数: {notification_count}"
    )

    print(
        f"保存ファイル: {STATE_FILE}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
