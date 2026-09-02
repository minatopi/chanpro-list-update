import os
import re
import json
import time
import base64
from datetime import datetime, timezone

import psycopg
import requests

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError
)


# ============================================================
# 設定
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_FILE_PATH = os.environ.get(
    "GITHUB_FILE_PATH",
    "program_stats.json"
)

# GitHub API
GITHUB_API_URL = (
    f"https://api.github.com/repos/"
    f"{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
)

# ページ読み込み後の待機時間
WAIT_AFTER_LOAD = 8000

# プロジェクトカード
CARD_SELECTOR = "div.clickable-element"

# カードコンテナ
CONTAINER_SELECTOR = (
    "div.bubble-element.Group.baTcwaH1"
)

# ローカルにも結果を保存
OUTPUT_FILE = "result.json"


# ============================================================
# GitHub API
# ============================================================

def github_headers():

    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"
    }


# ============================================================
# GitHubから前回JSONを取得
# ============================================================

def load_previous_stats():

    print()
    print("=" * 60)
    print("GitHubから前回データを取得")
    print("=" * 60)

    try:

        response = requests.get(
            GITHUB_API_URL,
            headers=github_headers(),
            timeout=30
        )

        # ----------------------------------------------------
        # JSONがまだ存在しない
        # ----------------------------------------------------

        if response.status_code == 404:

            print(
                "GitHubに前回JSONがありません。"
            )

            return {}, None

        # ----------------------------------------------------
        # その他エラー
        # ----------------------------------------------------

        response.raise_for_status()

        data = response.json()

        encoded_content = data.get(
            "content",
            ""
        )

        sha = data.get(
            "sha"
        )

        if not encoded_content:

            print(
                "GitHub JSONのcontentがありません。"
            )

            return {}, sha

        # ----------------------------------------------------
        # Base64デコード
        # ----------------------------------------------------

        decoded = base64.b64decode(
            encoded_content
        ).decode(
            "utf-8"
        )

        previous_data = json.loads(
            decoded
        )

        print(
            f"前回データ取得成功: "
            f"{len(previous_data.get('profiles', {}))}プロフィール"
        )

        return previous_data, sha

    except Exception as e:

        print(
            "GitHubからの取得に失敗しました:"
        )

        print(
            str(e)
        )

        raise


# ============================================================
# GitHubへJSON保存
# ============================================================

def save_stats_to_github(
    data,
    sha=None
):

    print()
    print("=" * 60)
    print("GitHubへ新しいJSONを保存")
    print("=" * 60)

    content = json.dumps(
        data,
        ensure_ascii=False,
        indent=2
    )

    encoded_content = base64.b64encode(
        content.encode("utf-8")
    ).decode("ascii")

    payload = {
        "message": (
            "Update program statistics "
            + data["updated_at"]
        ),
        "content": encoded_content
    }

    # --------------------------------------------------------
    # 既存ファイルの場合
    # --------------------------------------------------------

    if sha:

        payload["sha"] = sha

    try:

        response = requests.put(
            GITHUB_API_URL,
            headers=github_headers(),
            json=payload,
            timeout=30
        )

        response.raise_for_status()

        result = response.json()

        print(
            "GitHubへの保存成功"
        )

        print(
            "commit:",
            result.get("commit", {}).get("sha")
        )

        return True

    except Exception as e:

        print(
            "GitHubへの保存に失敗しました:"
        )

        print(
            str(e)
        )

        raise


# ============================================================
# テキスト整形
# ============================================================

def clean_text(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


# ============================================================
# いいね数
# ============================================================

def extract_like(text):

    if not text:
        return 0

    # --------------------------------------------------------
    # 「19こ」
    # --------------------------------------------------------

    match = re.search(
        r"(\d+)\s*こ",
        text
    )

    if match:

        return int(
            match.group(1)
        )

    return 0


# ============================================================
# 閲覧数
# ============================================================

def extract_progress(text):

    if not text:
        return 0, 0

    # --------------------------------------------------------
    # 「275/1000」
    # --------------------------------------------------------

    match = re.search(
        r"(\d+)\s*/\s*(\d+)",
        text
    )

    if match:

        current = int(
            match.group(1)
        )

        total = int(
            match.group(2)
        )

        return current, total

    return 0, 0


# ============================================================
# カード解析
# ============================================================

def parse_card(card):

    try:

        text_elements = card.locator(
            "div.bubble-element.Text"
        ).all_inner_texts()

        texts = []

        for text in text_elements:

            text = clean_text(
                text
            )

            if text:

                texts.append(
                    text
                )

        if not texts:

            return None

        # ----------------------------------------------------
        # プロジェクト名
        # ----------------------------------------------------

        title = texts[0]

        # ----------------------------------------------------
        # いいね
        # ----------------------------------------------------

        like = 0

        for text in texts:

            value = extract_like(
                text
            )

            if value:

                like = value

                break

        # ----------------------------------------------------
        # 閲覧数
        # ----------------------------------------------------

        views = 0
        total = 0

        for text in texts:

            current, max_value = (
                extract_progress(
                    text
                )
            )

            if current or max_value:

                views = current
                total = max_value

                break

        return {
            "title": title,
            "likes": like,
            "views": views,
            "total": total
        }

    except Exception as e:

        print(
            "カード解析エラー:",
            e
        )

        return None


# ============================================================
# プロフィール取得
# ============================================================

def scrape_profile(
    page,
    url
):

    print()
    print("-" * 60)
    print(
        "プロフィール取得:",
        url
    )
    print("-" * 60)

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except PlaywrightTimeoutError:

        print(
            "ページ読み込みタイムアウト"
        )

    except Exception as e:

        print(
            "ページ読み込みエラー:",
            e
        )

        return {
            "url": url,
            "status": "error",
            "projects": [],
            "total_likes": None,
            "total_views": None,
            "error": str(e)
        }

    # --------------------------------------------------------
    # Bubble描画待ち
    # --------------------------------------------------------

    page.wait_for_timeout(
        WAIT_AFTER_LOAD
    )

    # --------------------------------------------------------
    # コンテナ取得
    # --------------------------------------------------------

    try:

        container = page.locator(
            CONTAINER_SELECTOR
        ).first

        container.wait_for(
            state="visible",
            timeout=30000
        )

    except PlaywrightTimeoutError:

        print(
            "カードコンテナが見つかりませんでした"
        )

        return {
            "url": url,
            "status": "error",
            "projects": [],
            "total_likes": None,
            "total_views": None,
            "error": "container not found"
        }

    # --------------------------------------------------------
    # カード取得
    # --------------------------------------------------------

    cards = container.locator(
        CARD_SELECTOR
    )

    card_count = cards.count()

    print(
        f"プロジェクト数: {card_count}"
    )

    projects = []

    # --------------------------------------------------------
    # 各カード
    # --------------------------------------------------------

    for i in range(card_count):

        try:

            card = cards.nth(i)

            parsed = parse_card(
                card
            )

            if parsed:

                projects.append(
                    parsed
                )

                print(
                    f"[{i + 1}/{card_count}] "
                    f"{parsed['title']} "
                    f"like={parsed['likes']} "
                    f"views={parsed['views']}"
                )

            else:

                print(
                    f"[{i + 1}/{card_count}] "
                    "解析できませんでした"
                )

        except Exception as e:

            print(
                f"[{i + 1}/{card_count}] "
                f"エラー: {e}"
            )

    # --------------------------------------------------------
    # 合計
    # --------------------------------------------------------

    total_likes = sum(
        project["likes"]
        for project in projects
    )

    total_views = sum(
        project["views"]
        for project in projects
    )

    print()
    print(
        f"総いいね数: {total_likes}"
    )

    print(
        f"総閲覧数: {total_views}"
    )

    return {
        "url": url,
        "status": "success",
        "projects": projects,
        "total_likes": total_likes,
        "total_views": total_views
    }


# ============================================================
# DBから全プロフィールURL取得
# ============================================================

def get_all_programs(
    conn
):

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

                programs.add(
                    url.strip()
                )

    return sorted(
        programs
    )


# ============================================================
# 前回プロジェクトを辞書化
# ============================================================

def project_map(
    projects
):

    result = {}

    for project in projects or []:

        title = clean_text(
            project.get(
                "title",
                ""
            )
        )

        if not title:
            continue

        result[title] = project

    return result


# ============================================================
# いいね増加を検出
# ============================================================

def detect_like_increases(
    previous_profile,
    current_profile
):

    notifications = []

    previous_projects = project_map(
        previous_profile.get(
            "projects",
            []
        )
        if previous_profile
        else []
    )

    current_projects = project_map(
        current_profile.get(
            "projects",
            []
        )
    )

    for title, current in current_projects.items():

        current_likes = int(
            current.get(
                "likes",
                0
            )
        )

        previous = previous_projects.get(
            title
        )

        # ----------------------------------------------------
        # 初めて発見したプロジェクト
        #
        # 初回は通知しない
        # ----------------------------------------------------

        if previous is None:

            print(
                f"新規プロジェクト: "
                f"{title}"
            )

            continue

        previous_likes = int(
            previous.get(
                "likes",
                0
            )
        )

        # ----------------------------------------------------
        # 増加
        # ----------------------------------------------------

        if current_likes > previous_likes:

            increase = (
                current_likes
                - previous_likes
            )

            print(
                "いいね増加:",
                title,
                f"{previous_likes} -> "
                f"{current_likes}",
                f"(+{increase})"
            )

            notifications.append({
                "title": title,
                "old_likes": previous_likes,
                "new_likes": current_likes,
                "increase": increase
            })

    return notifications


# ============================================================
# 通知登録
# ============================================================

def create_notifications(
    conn,
    user_id,
    notifications
):

    created_count = 0

    for notification in notifications:

        title = notification[
            "title"
        ]

        new_likes = notification[
            "new_likes"
        ]

        message = (
            f"「{title}」が"
            f"{new_likes}いいねされました"
        )

        # ----------------------------------------------------
        # 直近10分以内の同じ通知を確認
        # ----------------------------------------------------

        with conn.cursor() as cur:

            cur.execute("""
                SELECT id
                FROM public.notifications
                WHERE user_id = %s
                  AND type = 'like'
                  AND message = %s
                  AND created_at >= NOW()
                      - INTERVAL '10 minutes'
                LIMIT 1
            """, (
                user_id,
                message
            ))

            exists = cur.fetchone()

            if exists:

                print(
                    "重複通知をスキップ:",
                    message
                )

                continue

            # ------------------------------------------------
            # 通知登録
            # ------------------------------------------------

            cur.execute("""
                INSERT INTO public.notifications (
                    user_id,
                    actor_id,
                    type,
                    post_id,
                    message_id,
                    is_read,
                    message
                )
                VALUES (
                    %s,
                    NULL,
                    'like',
                    NULL,
                    NULL,
                    FALSE,
                    %s
                )
            """, (
                user_id,
                message
            ))

        print(
            "通知作成:",
            message
        )

        created_count += 1

    return created_count


# ============================================================
# users更新
# ============================================================

def update_users(
    conn,
    stats
):

    updated_users = 0
    notification_count = 0

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

        for (
            user_id,
            program_urls,
            old_views,
            old_likes
        ) in rows:

            program_urls = (
                program_urls or []
            )

            old_views = (
                old_views or []
            )

            old_likes = (
                old_likes or []
            )

            new_views = []
            new_likes = []

            # ------------------------------------------------
            # URLごと
            # ------------------------------------------------

            for index, url in enumerate(
                program_urls
            ):

                url = url.strip()

                data = stats.get(
                    url
                )

                # ------------------------------------------------
                # 取得失敗
                # ------------------------------------------------

                if (
                    data is None
                    or data.get("status")
                    != "success"
                ):

                    previous_view = (
                        old_views[index]
                        if index < len(old_views)
                        else 0
                    )

                    previous_like = (
                        old_likes[index]
                        if index < len(old_likes)
                        else 0
                    )

                    new_views.append(
                        previous_view
                    )

                    new_likes.append(
                        previous_like
                    )

                    print(
                        "取得失敗のため"
                        "既存値を維持:",
                        url
                    )

                    continue

                # ------------------------------------------------
                # 今回値
                # ------------------------------------------------

                current_views = data[
                    "total_views"
                ]

                current_likes = data[
                    "total_likes"
                ]

                new_views.append(
                    current_views
                )

                new_likes.append(
                    current_likes
                )

            # ------------------------------------------------
            # users更新
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
                user_id
            ))

            updated_users += 1

    conn.commit()

    return (
        updated_users,
        notification_count
    )


# ============================================================
# users更新 + 通知
# ============================================================

def update_users_and_notifications(
    conn,
    stats,
    previous_data
):

    updated_users = 0
    notification_count = 0

    previous_profiles = (
        previous_data.get(
            "profiles",
            {}
        )
        if previous_data
        else {}
    )

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

        for (
            user_id,
            program_urls,
            old_views,
            old_likes
        ) in rows:

            program_urls = (
                program_urls or []
            )

            old_views = (
                old_views or []
            )

            old_likes = (
                old_likes or []
            )

            new_views = []
            new_likes = []

            # ------------------------------------------------
            # URLごと
            # ------------------------------------------------

            for index, url in enumerate(
                program_urls
            ):

                url = url.strip()

                current_profile = stats.get(
                    url
                )

                # ------------------------------------------------
                # 取得失敗
                # ------------------------------------------------

                if (
                    current_profile is None
                    or current_profile.get(
                        "status"
                    ) != "success"
                ):

                    previous_view = (
                        old_views[index]
                        if index < len(old_views)
                        else 0
                    )

                    previous_like = (
                        old_likes[index]
                        if index < len(old_likes)
                        else 0
                    )

                    new_views.append(
                        previous_view
                    )

                    new_likes.append(
                        previous_like
                    )

                    continue

                # ------------------------------------------------
                # 今回値
                # ------------------------------------------------

                current_views = (
                    current_profile[
                        "total_views"
                    ]
                )

                current_likes = (
                    current_profile[
                        "total_likes"
                    ]
                )

                new_views.append(
                    current_views
                )

                new_likes.append(
                    current_likes
                )

                # ------------------------------------------------
                # GitHubの前回値
                # ------------------------------------------------

                previous_profile = (
                    previous_profiles.get(
                        url
                    )
                )

                if previous_profile:

                    increases = (
                        detect_like_increases(
                            previous_profile,
                            current_profile
                        )
                    )

                    if increases:

                        count = (
                            create_notifications(
                                conn,
                                user_id,
                                increases
                            )
                        )

                        notification_count += (
                            count
                        )

            # ------------------------------------------------
            # users更新
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
                user_id
            ))

            updated_users += 1

    conn.commit()

    return (
        updated_users,
        notification_count
    )


# ============================================================
# メイン
# ============================================================

def main():

    start_time = time.time()

    now = datetime.now(
        timezone.utc
    )

    updated_at = now.isoformat()

    print("=" * 60)
    print(
        "プログラム統計・いいね通知処理"
    )
    print("=" * 60)

    # ========================================================
    # GitHub前回値
    # ========================================================

    previous_data, github_sha = (
        load_previous_stats()
    )

    # ========================================================
    # DB
    # ========================================================

    with psycopg.connect(
        DATABASE_URL
    ) as conn:

        # ----------------------------------------------------
        # URL取得
        # ----------------------------------------------------

        programs = get_all_programs(
            conn
        )

        print()
        print(
            f"プロフィールURL数: "
            f"{len(programs)}"
        )

        # ----------------------------------------------------
        # Playwright
        # ----------------------------------------------------

        stats = {}

        with sync_playwright() as p:

            print(
                "ブラウザ起動中..."
            )

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
                )
            )

            # ------------------------------------------------
            # 全プロフィール
            # ------------------------------------------------

            for url in programs:

                result = scrape_profile(
                    page,
                    url
                )

                stats[url] = result

            browser.close()

        # ====================================================
        # 通知 + users更新
        # ====================================================

        (
            updated_users,
            notification_count
        ) = update_users_and_notifications(
            conn,
            stats,
            previous_data
        )

    # ========================================================
    # GitHub保存用データ
    # ========================================================

    github_data = {
        "updated_at": updated_at,

        "profile_count": len(
            stats
        ),

        "profiles": {}
    }

    # --------------------------------------------------------
    # 成功したプロフィールだけ保存
    # --------------------------------------------------------

    for url, profile in stats.items():

        if profile.get(
            "status"
        ) != "success":

            # 失敗した場合は前回値を維持
            previous_profile = (
                previous_data
                .get("profiles", {})
                .get(url)
            )

            if previous_profile:

                github_data[
                    "profiles"
                ][url] = previous_profile

            continue

        github_data[
            "profiles"
        ][url] = {
            "url": url,
            "projects": profile[
                "projects"
            ],
            "total_likes": profile[
                "total_likes"
            ],
            "total_views": profile[
                "total_views"
            ]
        }

    # ========================================================
    # GitHubへ保存
    # ========================================================

    save_stats_to_github(
        github_data,
        github_sha
    )

    # ========================================================
    # ローカルにも保存
    # ========================================================

    local_result = {
        "updated_at": updated_at,
        "profile_count": len(
            stats
        ),
        "updated_users": updated_users,
        "notification_count": (
            notification_count
        ),
        "profiles": stats
    }

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            local_result,
            f,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # 結果
    # ========================================================

    elapsed = (
        time.time()
        - start_time
    )

    print()
    print("=" * 60)
    print("処理完了")
    print("=" * 60)

    print(
        f"プロフィール数: "
        f"{len(programs)}"
    )

    print(
        f"ユーザー更新数: "
        f"{updated_users}"
    )

    print(
        f"作成した通知数: "
        f"{notification_count}"
    )

    print(
        f"処理時間: "
        f"{elapsed:.2f}秒"
    )

    print(
        f"GitHub JSON: "
        f"{GITHUB_FILE_PATH}"
    )

    print(
        f"ローカル結果: "
        f"{OUTPUT_FILE}"
    )

    print("=" * 60)


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":

    main()
