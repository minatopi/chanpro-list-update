import os
import re
import json
import subprocess
from datetime import datetime, timezone

import psycopg
from playwright.sync_api import sync_playwright


# ============================================================
# 設定
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_FILE = os.path.join(BASE_DIR, "program_stats.json")

WAIT_AFTER_LOAD = 8000

CARD_SELECTOR = "div.clickable-element"


# ============================================================
# 時刻
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# Git
# ============================================================

def run_git(*args, check=True):
    cmd = ["git"] + list(args)

    print(">", " ".join(cmd))

    result = subprocess.run(
        cmd,
        cwd=BASE_DIR,
        text=True,
        capture_output=True
    )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print(result.stderr)

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Git command failed: {' '.join(cmd)}"
        )

    return result


def configure_git():

    print("Gitユーザー設定...")

    run_git(
        "config",
        "user.name",
        "github-actions[bot]"
    )

    run_git(
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com"
    )


# ============================================================
# JSON
# ============================================================

def load_previous_stats():

    if not os.path.exists(JSON_FILE):
        print("program_stats.json がありません。新規作成します。")

        return {
            "updated_at": None,
            "profiles": {}
        }

    try:

        with open(
            JSON_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("JSONの形式が不正です")

        if "profiles" not in data:
            data["profiles"] = {}

        return data

    except Exception as e:

        print(
            "program_stats.json の読み込みに失敗:",
            e
        )

        print("空のデータとして開始します。")

        return {
            "updated_at": None,
            "profiles": {}
        }


def save_stats(data):

    data["updated_at"] = now_iso()

    temp_file = JSON_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

        f.write("\n")

    os.replace(
        temp_file,
        JSON_FILE
    )

    print(
        "program_stats.json を保存しました。"
    )


# ============================================================
# 数字抽出
# ============================================================

def extract_numbers(text):

    if not text:
        return []

    text = text.replace(",", "")

    return [
        int(x)
        for x in re.findall(
            r"\d+",
            text
        )
    ]


# ============================================================
# SVGアイコンから数字取得
# ============================================================

def get_icon_number_from_card(card, icon_name):

    selectors = [
        f'use[href="#{icon_name}"]',
        f'use[xlink\\:href="#{icon_name}"]',
        f'svg use[href="#{icon_name}"]',
        f'svg use[xlink\\:href="#{icon_name}"]',
    ]

    icon = None

    for selector in selectors:

        try:

            loc = card.locator(selector)

            if loc.count() > 0:

                icon = loc.first
                break

        except Exception:
            pass

    if icon is None:
        return 0

    # --------------------------------------------------------
    # アイコンの親を順番に辿る
    # --------------------------------------------------------

    current = icon

    for level in range(1, 10):

        try:

            current = current.locator("xpath=..")

            text = current.inner_text(
                timeout=1000
            ).strip()

            if not text:
                continue

            numbers = extract_numbers(text)

            if numbers:

                # アイコン周辺の数字を取得
                return numbers[-1]

        except Exception:
            continue

    return 0


# ============================================================
# カードからタイトル取得
# ============================================================

def get_card_title(card):

    try:

        texts = card.locator(
            "div.bubble-element.Text"
        ).all_inner_texts()

        texts = [
            x.strip()
            for x in texts
            if x.strip()
        ]

        if texts:
            return texts[0]

    except Exception:
        pass

    try:

        text = card.inner_text()

        lines = [
            x.strip()
            for x in text.splitlines()
            if x.strip()
        ]

        if lines:
            return lines[0]

    except Exception:
        pass

    return "名称不明"


# ============================================================
# カードから安定したIDを探す
# ============================================================

def get_card_id(card):

    # --------------------------------------------------------
    # hrefからIDを取得
    # --------------------------------------------------------

    try:

        links = card.locator("a")

        count = links.count()

        for i in range(count):

            href = links.nth(i).get_attribute("href")

            if href:

                # 数字だけのID
                match = re.search(
                    r"/(\d{10,})",
                    href
                )

                if match:
                    return match.group(1)

                # UUID
                match = re.search(
                    r"/([0-9a-fA-F-]{20,})",
                    href
                )

                if match:
                    return match.group(1)

    except Exception:
        pass

    # --------------------------------------------------------
    # data-idなど
    # --------------------------------------------------------

    for attr in [
        "data-id",
        "data-log-id",
        "data-record-id",
        "data-item-id"
    ]:

        try:

            value = card.get_attribute(attr)

            if value:
                return value

        except Exception:
            pass

    return None


# ============================================================
# プロジェクトカード解析
# ============================================================

def parse_project_card(card):

    title = get_card_title(card)

    likes = get_icon_number_from_card(
        card,
        "heart"
    )

    views = get_icon_number_from_card(
        card,
        "eyes"
    )

    card_id = get_card_id(card)

    print(
        f"    {title} : "
        f"いいね={likes} "
        f"閲覧={views}"
        + (
            f" ID={card_id}"
            if card_id
            else ""
        )
    )

    return {
        "id": card_id,
        "title": title,
        "likes": likes,
        "views": views
    }


# ============================================================
# プロフィール解析
# ============================================================

def scrape_profile(page, url):

    print()
    print("=" * 70)
    print("プロフィール:", url)
    print("=" * 70)

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000
    )

    page.wait_for_timeout(
        WAIT_AFTER_LOAD
    )

    cards = page.locator(
        CARD_SELECTOR
    )

    card_count = cards.count()

    print(
        "カード数:",
        card_count
    )

    projects = []

    for i in range(card_count):

        try:

            card = cards.nth(i)

            project = parse_project_card(
                card
            )

            # 明らかな空カードを除外
            if (
                project["title"] == "名称不明"
                and project["likes"] == 0
                and project["views"] == 0
            ):
                continue

            projects.append(
                project
            )

        except Exception as e:

            print(
                f"カード {i + 1} の解析失敗:",
                e
            )

    print(
        "取得プロジェクト数:",
        len(projects)
    )

    return projects


# ============================================================
# DB接続
# ============================================================

def get_users(conn):

    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                id,
                username,
                program_urls
            FROM public.users
            WHERE program_urls IS NOT NULL
              AND array_length(program_urls, 1) > 0
        """)

        return cur.fetchall()


# ============================================================
# 通知登録
# ============================================================

def create_like_notification(
    conn,
    user_id,
    title
):

    message = f"「{title}」がいいねされました"

    with conn.cursor() as cur:

        # 同じ通知を短時間に大量生成しない
        cur.execute("""
            SELECT id
            FROM public.notifications
            WHERE user_id = %s
              AND type = 'like'
              AND message = %s
              AND created_at > NOW() - INTERVAL '10 minutes'
            LIMIT 1
        """, (
            user_id,
            message
        ))

        exists = cur.fetchone()

        if exists:

            print(
                "    通知は既に存在:",
                message
            )

            return

        cur.execute("""
            INSERT INTO public.notifications
            (
                user_id,
                type,
                message,
                is_read
            )
            VALUES
            (
                %s,
                'like',
                %s,
                false
            )
        """, (
            user_id,
            message
        ))

        print(
            "    ★ いいね通知:",
            message
        )


# ============================================================
# いいね増加チェック
# ============================================================

def check_like_increase(
    conn,
    user_id,
    previous_projects,
    current_projects
):

    # --------------------------------------------------------
    # ID優先
    # IDがない場合はタイトルをキーにする
    # --------------------------------------------------------

    previous_map = {}

    for project in previous_projects:

        project_id = project.get("id")

        if project_id:

            key = f"id:{project_id}"

        else:

            key = f"title:{project.get('title', '')}"

        previous_map[key] = project

    for current in current_projects:

        project_id = current.get("id")

        if project_id:

            key = f"id:{project_id}"

        else:

            key = f"title:{current.get('title', '')}"

        previous = previous_map.get(key)

        if previous is None:

            print(
                f"    新規プロジェクト: "
                f"{current['title']}"
            )

            # 初回登録時は通知しない
            continue

        old_likes = int(
            previous.get("likes", 0)
        )

        new_likes = int(
            current.get("likes", 0)
        )

        if new_likes > old_likes:

            diff = new_likes - old_likes

            print(
                f"    ★ いいね増加 "
                f"{current['title']} "
                f"{old_likes} → {new_likes} "
                f"(+{diff})"
            )

            create_like_notification(
                conn,
                user_id,
                current["title"]
            )


# ============================================================
# ユーザーDB更新
# ============================================================

def update_user_stats(
    conn,
    user_id,
    profile_stats
):

    # --------------------------------------------------------
    # プロフィール全体の合計
    # --------------------------------------------------------

    total_likes = sum(
        int(x.get("likes", 0))
        for x in profile_stats
    )

    total_views = sum(
        int(x.get("views", 0))
        for x in profile_stats
    )

    print(
        "    合計:",
        "いいね=", total_likes,
        "閲覧=", total_views
    )

    # --------------------------------------------------------
    # 既存の配列形式を壊さないため、
    # プロフィール単位の値を1要素配列として保存
    #
    # ※ program_views / program_likes を
    # URLごとの値として使っている場合は、
    # この部分だけ元仕様に合わせて変更してください。
    # --------------------------------------------------------

    with conn.cursor() as cur:

        cur.execute("""
            UPDATE public.users
            SET
                program_views = %s,
                program_likes = %s
            WHERE id = %s
        """, (
            [total_views],
            [total_likes],
            user_id
        ))


# ============================================================
# メイン
# ============================================================

def main():

    print()
    print("=" * 70)
    print("Chanpro プロジェクト統計更新")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # 前回データ
    # --------------------------------------------------------

    previous_data = load_previous_stats()

    previous_profiles = previous_data.get(
        "profiles",
        {}
    )

    # --------------------------------------------------------
    # DB
    # --------------------------------------------------------

    print("PostgreSQLへ接続...")

    conn = psycopg.connect(
        DATABASE_URL
    )

    print("DB接続成功")

    try:

        users = get_users(conn)

        print(
            "対象ユーザー:",
            len(users)
        )

        # ----------------------------------------------------
        # Playwright
        # ----------------------------------------------------

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            page = browser.new_page(
                viewport={
                    "width": 1440,
                    "height": 1000
                }
            )

            # ------------------------------------------------
            # URLごとに一度だけスクレイピング
            # ------------------------------------------------

            profile_cache = {}

            for user_id, username, program_urls in users:

                print()
                print(
                    "ユーザー:",
                    username,
                    user_id
                )

                if not program_urls:
                    continue

                for url in program_urls:

                    if not url:
                        continue

                    if url not in profile_cache:

                        try:

                            projects = scrape_profile(
                                page,
                                url
                            )

                            profile_cache[url] = projects

                        except Exception as e:

                            print(
                                "プロフィール取得失敗:",
                                url,
                                e
                            )

                            profile_cache[url] = []

                    projects = profile_cache[url]

                    # ----------------------------------------
                    # 前回値
                    # ----------------------------------------

                    old_profile = (
                        previous_profiles
                        .get(url, {})
                    )

                    old_projects = (
                        old_profile
                        .get("projects", [])
                    )

                    # ----------------------------------------
                    # いいね増加確認
                    # ----------------------------------------

                    check_like_increase(
                        conn,
                        user_id,
                        old_projects,
                        projects
                    )

            browser.close()

        # ----------------------------------------------------
        # JSONを作り直す
        # ----------------------------------------------------

        new_profiles = {}

        for url, projects in profile_cache.items():

            new_profiles[url] = {
                "updated_at": now_iso(),
                "projects": projects
            }

        new_data = {
            "updated_at": now_iso(),
            "profiles": new_profiles
        }

        save_stats(
            new_data
        )

        # ----------------------------------------------------
        # DBコミット
        # ----------------------------------------------------

        conn.commit()

        print()
        print("DB更新をコミットしました。")

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()

    # ========================================================
    # GitHub
    # ========================================================

    print()
    print("=" * 70)
    print("GitHubへ保存")
    print("=" * 70)

    configure_git()

    status = run_git(
        "status",
        "--porcelain"
    )

    if not status.stdout.strip():

        print(
            "変更がないためGit commitは不要です。"
        )

        return

    print(
        "変更ファイル:"
    )

    print(
        status.stdout
    )

    run_git(
        "add",
        "program_stats.json"
    )

    commit_result = run_git(
        "commit",
        "-m",
        "Update program stats",
        check=False
    )

    if commit_result.returncode != 0:

        # commit対象がない場合
        if "nothing to commit" in (
            commit_result.stdout
            + commit_result.stderr
        ).lower():

            print(
                "commit対象がありません。"
            )

            return

        raise RuntimeError(
            "git commitに失敗しました"
        )

    run_git(
        "push"
    )

    print()
    print(
        "GitHubへのpush完了"
    )


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":

    main()
