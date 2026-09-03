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

# GitHub Actionsで実行する場合、
# リポジトリ内にこのJSONを置く
STATE_FILE = "program_likes.json"


# ============================================================
# JSON読み込み
# ============================================================

def load_state():
    """
    GitHubに保存している前回のいいね数を読み込む
    """

    if not os.path.exists(STATE_FILE):
        print("前回のJSONがありません。初回実行として扱います。")

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

            data = json.load(f)

        if "users" not in data:
            data["users"] = {}

        return data

    except Exception as e:

        print("JSON読み込み失敗:", e)

        return {
            "updated_at": None,
            "users": {}
        }


# ============================================================
# JSON保存
# ============================================================

def save_state(state):

    state["updated_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# アイコンから数字取得
# ============================================================

def get_number_from_icon(page, icon_name):
    """
    #eyes / #heart のアイコンから数字を取得
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
            f"数字取得エラー [{icon_name}]:",
            e
        )

        return None


# ============================================================
# タイトル取得
# ============================================================

def get_program_title(page):
    """
    作品ページから作品タイトルを取得する。

    h1を優先。
    見つからない場合はページ内の文字から候補を探す。
    """

    # --------------------------------------------------------
    # h1
    # --------------------------------------------------------

    try:

        h1 = page.locator("h1").first

        if h1.count() > 0:

            text = h1.inner_text(
                timeout=3000
            ).strip()

            if text:
                return text

    except Exception:
        pass

    # --------------------------------------------------------
    # titleタグ
    # --------------------------------------------------------

    try:

        title = page.title().strip()

        if title:

            # サイト名などを除去
            title = re.sub(
                r"\s*[\|\-｜－]\s*.*$",
                "",
                title
            ).strip()

            if title:
                return title

    except Exception:
        pass

    return None


# ============================================================
# 作品情報取得
# ============================================================

def get_program_data(page, url):

    print()
    print("取得中:")
    print(url)

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        # Bubbleの描画を待つ
        page.wait_for_timeout(5000)

        likes = get_number_from_icon(
            page,
            "heart"
        )

        views = get_number_from_icon(
            page,
            "eyes"
        )

        title = get_program_title(page)

        # タイトルが取れなかった場合
        if not title:
            title = "作品"

        result = {
            "url": url,
            "title": title,
            "likes": likes,
            "views": views,
            "status": "success"
        }

        print(
            f"  タイトル: {title}"
        )

        print(
            f"  いいね: {likes}"
        )

        print(
            f"  閲覧数: {views}"
        )

        return result

    except Exception as e:

        print(
            "取得失敗:",
            url
        )

        print(
            str(e)
        )

        return {
            "url": url,
            "title": None,
            "likes": None,
            "views": None,
            "status": "error",
            "error": str(e)
        }


# ============================================================
# 全ユーザーの作品を取得
# ============================================================

def get_all_user_programs(conn):

    """
    ユーザーごとに全作品を取得する。

    戻り値:

    {
        user_id: [
            url,
            url,
            ...
        ]
    }
    """

    result = {}

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

        urls = []

        for url in program_urls:

            if not url:
                continue

            url = url.strip()

            if url:
                urls.append(url)

        if urls:
            result[str(user_id)] = urls

    return result


# ============================================================
# URL → 実際に所有しているユーザー
# ============================================================

def get_user_program_map(user_programs):

    """
    同じ作品URLを複数ユーザーが持っていても対応できるようにする。

    {
        url: [
            user_id,
            user_id
        ]
    }
    """

    result = {}

    for user_id, urls in user_programs.items():

        for url in urls:

            if url not in result:
                result[url] = []

            result[url].append(
                user_id
            )

    return result


# ============================================================
# いいね通知
# ============================================================

def create_like_notification(
    conn,
    user_id,
    title,
    increase
):

    message = f"「{title}」にいいねがされました"

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
            "★ 通知作成"
        )

        print(
            f"  ユーザーID: {user_id}"
        )

        print(
            f"  通知: {message}"
        )

        print(
            f"  増加数: +{increase}"
        )

        return True

    except Exception as e:

        print()
        print(
            "★★ 通知INSERT失敗 ★★"
        )

        print(
            f"  ユーザーID: {user_id}"
        )

        print(
            f"  メッセージ: {message}"
        )

        print(
            f"  エラー: {e}"
        )

        return False


# ============================================================
# メイン
# ============================================================

def main():

    fetched_at = datetime.now(
        timezone.utc
    ).isoformat()

    print("=" * 70)
    print("作品いいね数チェック開始")
    print("=" * 70)

    # --------------------------------------------------------
    # 前回データ
    # --------------------------------------------------------

    state = load_state()

    old_users = state.get(
        "users",
        {}
    )

    print()
    print(
        f"前回JSONのユーザー数: {len(old_users)}"
    )

    # --------------------------------------------------------
    # DB接続
    # --------------------------------------------------------

    with psycopg.connect(
        DATABASE_URL
    ) as conn:

        # ----------------------------------------------------
        # ユーザーごとの作品一覧
        # ----------------------------------------------------

        user_programs = get_all_user_programs(
            conn
        )

        print()
        print(
            f"ユーザー数: {len(user_programs)}"
        )

        total_urls = sum(
            len(urls)
            for urls in user_programs.values()
        )

        print(
            f"作品登録数: {total_urls}"
        )

        # ----------------------------------------------------
        # URLを重複排除
        # ----------------------------------------------------

        all_urls = set()

        for urls in user_programs.values():

            for url in urls:

                all_urls.add(url)

        all_urls = sorted(all_urls)

        print(
            f"実際に取得するURL数: {len(all_urls)}"
        )

        # ----------------------------------------------------
        # URL → ユーザー
        # ----------------------------------------------------

        url_users = get_user_program_map(
            user_programs
        )

        # ====================================================
        # Playwright
        # ====================================================

        current_data = {}

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

            for url in all_urls:

                result = get_program_data(
                    page,
                    url
                )

                current_data[url] = result

            browser.close()

        # ====================================================
        # いいね数比較
        # ====================================================

        notification_count = 0

        print()
        print("=" * 70)
        print("いいね数比較")
        print("=" * 70)

        for url in all_urls:

            current = current_data.get(url)

            if not current:
                continue

            if current["status"] != "success":
                continue

            new_likes = current["likes"]

            if new_likes is None:
                continue

            title = current.get(
                "title"
            )

            if not title:
                title = "作品"

            # ------------------------------------------------
            # この作品を持っているユーザー
            # ------------------------------------------------

            users = url_users.get(
                url,
                []
            )

            for user_id in users:

                user_old_data = old_users.get(
                    user_id,
                    {}
                )

                old_programs = user_old_data.get(
                    "programs",
                    {}
                )

                old_program = old_programs.get(
                    url
                )

                # ------------------------------------------------
                # 初回
                # ------------------------------------------------

                if old_program is None:

                    print()
                    print(
                        "初回登録:"
                    )

                    print(
                        f"  ユーザー: {user_id}"
                    )

                    print(
                        f"  作品: {title}"
                    )

                    print(
                        f"  いいね: {new_likes}"
                    )

                    continue

                old_likes = old_program.get(
                    "likes",
                    0
                )

                if old_likes is None:
                    old_likes = 0

                # ------------------------------------------------
                # 増加
                # ------------------------------------------------

                if new_likes > old_likes:

                    increase = (
                        new_likes
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
                        f"  今回: {new_likes}"
                    )

                    print(
                        f"  増加: +{increase}"
                    )

                    # ------------------------------------------------
                    # 通知
                    # ------------------------------------------------

                    created = create_like_notification(
                        conn=conn,
                        user_id=user_id,
                        title=title,
                        increase=increase
                    )

                    if created:

                        notification_count += 1

        # --------------------------------------------------------
        # 通知INSERTを確定
        # --------------------------------------------------------

        conn.commit()

        print()
        print(
            "notifications の変更をcommitしました。"
        )

    # ========================================================
    # 今回のデータをJSONへ保存
    # ========================================================

    new_state = {
        "updated_at": fetched_at,
        "users": {}
    }

    # --------------------------------------------------------
    # ユーザーごとに保存
    # --------------------------------------------------------

    for user_id, urls in user_programs.items():

        new_state["users"][user_id] = {
            "programs": {}
        }

        for url in urls:

            data = current_data.get(url)

            if not data:
                continue

            if data["status"] != "success":
                continue

            new_state["users"][user_id][
                "programs"
            ][url] = {
                "title": data.get(
                    "title"
                ),
                "likes": data.get(
                    "likes"
                ),
                "views": data.get(
                    "views"
                )
            }

    # --------------------------------------------------------
    # JSON保存
    # --------------------------------------------------------

    save_state(
        new_state
    )

    # ========================================================
    # 結果
    # ========================================================

    print()
    print("=" * 70)
    print("完了")
    print("=" * 70)

    print(
        f"ユーザー数: {len(user_programs)}"
    )

    print(
        f"作品URL数: {len(all_urls)}"
    )

    print(
        f"作成通知数: {notification_count}"
    )

    print(
        f"JSON: {STATE_FILE}"
    )

    print("=" * 70)


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":
    main()
