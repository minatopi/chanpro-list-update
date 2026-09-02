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
# 基本設定
# ============================================================

BASE_URL = "https://chanpro.jp"

PROFILE_PAGE_WAIT = 5000
PROGRAM_PAGE_WAIT = 3000

RESULT_FILE = "like_notification_result.json"


# ============================================================
# URL正規化
# ============================================================

def normalize_url(url):
    if not url:
        return None

    url = str(url).strip()

    if not url:
        return None

    if url.startswith("/"):
        return BASE_URL + url

    return url


# ============================================================
# URLがプロフィールURLか
# ============================================================

def is_profile_url(url):
    if not url:
        return False

    url = normalize_url(url)

    return (
        "/00-user-profile/" in url
        or "/user-profile/" in url
        or "user-profile" in url
    )


# ============================================================
# URLがプログラムURLか
# ============================================================

def is_program_url(url):
    if not url:
        return False

    url = normalize_url(url)

    return (
        "/00-program-profile/" in url
        or "/program-profile/" in url
        or "program-profile" in url
    )


# ============================================================
# 数字を安全に整数へ
# ============================================================

def safe_int(value, default=0):

    try:
        return int(value)
    except Exception:
        return default


# ============================================================
# プロフィールページからユーザー名を取得
# ============================================================

def get_profile_username(page):

    """
    プロフィール上部のユーザー名を取得。

    例:
        こうくん
        みなと
        test123

    固定の名前は使用しない。
    """

    # --------------------------------------------------------
    # サンプルHTMLでは、
    #
    # Group baTcyl1
    #   └ Text
    #       └ ユーザー名
    #
    # という構造になっている。
    #
    # Bubbleのクラス名は変わる可能性があるため、
    # 複数の方法で探す。
    # --------------------------------------------------------

    candidates = []

    # 方法1:
    # プロフィール上部の比較的大きなGroupから探す
    groups = page.locator(
        "div.bubble-element.Group"
    ).all()

    for group in groups[:100]:

        try:

            text_elements = group.locator(
                "div.bubble-element.Text"
            ).all()

            if not text_elements:
                continue

            for text_element in text_elements[:5]:

                text = text_element.inner_text().strip()

                if not text:
                    continue

                if "\n" in text:
                    continue

                if text in [
                    "ログイン",
                    "新規登録",
                    "プロフィール",
                ]:
                    continue

                if re.fullmatch(
                    r"[\d,]+",
                    text
                ):
                    continue

                # 数字を大量に含むものは名前ではない可能性が高い
                if len(re.findall(r"\d", text)) > 4:
                    continue

                # 極端に長い文章はプロフィール本文
                if len(text) > 50:
                    continue

                candidates.append(text)

        except Exception:
            continue

    # 最初の候補
    if candidates:
        return candidates[0]

    # --------------------------------------------------------
    # 最終手段
    # --------------------------------------------------------

    try:

        texts = page.locator(
            "div.bubble-element.Text"
        ).all_inner_texts()

        for text in texts:

            text = text.strip()

            if not text:
                continue

            if text in [
                "ログイン",
                "新規登録",
                "プロフィール",
            ]:
                continue

            if len(text) > 50:
                continue

            if re.fullmatch(
                r"[\d,]+",
                text
            ):
                continue

            return text

    except Exception:
        pass

    return None


# ============================================================
# プロフィールURLをプログラムページから探す
# ============================================================

def find_profile_url_from_program(
    page,
    program_url
):

    print(
        f"プロフィールURL探索: {program_url}"
    )

    try:

        page.goto(
            program_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(
            PROGRAM_PAGE_WAIT
        )

        # ----------------------------------------------------
        # hrefを全部取得
        # ----------------------------------------------------

        links = page.locator(
            "a[href]"
        ).all()

        for link in links:

            try:

                href = link.get_attribute(
                    "href"
                )

                if not href:
                    continue

                href = normalize_url(href)

                if is_profile_url(href):

                    print(
                        f"プロフィールURL発見: {href}"
                    )

                    return href

            except Exception:
                continue

        # ----------------------------------------------------
        # onclick等にURLが入っている可能性
        # ----------------------------------------------------

        html = page.content()

        patterns = [
            r'https://chanpro\.jp/[^"\']*user-profile[^"\']*',
            r'/[^"\']*user-profile/[^"\']*',
        ]

        for pattern in patterns:

            matches = re.findall(
                pattern,
                html
            )

            for match in matches:

                url = normalize_url(match)

                if is_profile_url(url):

                    print(
                        f"プロフィールURL発見: {url}"
                    )

                    return url

    except Exception as e:

        print(
            f"プロフィールURL探索失敗: {e}"
        )

    return None


# ============================================================
# ユーザーのプロフィールURLを取得
# ============================================================

def get_user_profile_url(
    page,
    user
):

    # --------------------------------------------------------
    # 1. users.profile にURLが入っている場合
    # --------------------------------------------------------

    profile = user.get("profile")

    if profile:

        profile = str(profile).strip()

        if is_profile_url(profile):

            return normalize_url(profile)

        # profileそのものがURLなら使う
        if profile.startswith("http"):

            return normalize_url(profile)

    # --------------------------------------------------------
    # 2. program_urlsから探す
    # --------------------------------------------------------

    program_urls = user.get(
        "program_urls"
    ) or []

    for program_url in program_urls:

        program_url = normalize_url(
            program_url
        )

        if not is_program_url(
            program_url
        ):
            continue

        profile_url = (
            find_profile_url_from_program(
                page,
                program_url
            )
        )

        if profile_url:
            return profile_url

    return None


# ============================================================
# プロジェクトカード解析
# ============================================================

def parse_card(
    text,
    profile_username=None
):

    """
    プロジェクトカードから

        title
        like
        views

    を取得する。
    """

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    if not lines:
        return None

    # --------------------------------------------------------
    # プロフィールユーザー名を除外
    # --------------------------------------------------------

    if profile_username:

        lines = [
            line
            for line in lines
            if line != profile_username
        ]

    # --------------------------------------------------------
    # 不要な文字
    # --------------------------------------------------------

    skip_exact = {
        "ログイン",
        "新規登録",
        "プロフィール",
    }

    lines = [
        line
        for line in lines
        if line not in skip_exact
    ]

    if not lines:
        return None

    # --------------------------------------------------------
    # タイトル
    # --------------------------------------------------------

    title = lines[0]

    # 数字取得
    #
    # 例:
    #   19こ
    #   275/1000
    #
    # → 19, 275, 1000
    # --------------------------------------------------------

    nums = re.findall(
        r"\d[\d,]*",
        text
    )

    numbers = [
        safe_int(
            n.replace(",", "")
        )
        for n in nums
    ]

    # --------------------------------------------------------
    # 最低でもいいね数が必要
    # --------------------------------------------------------

    if len(numbers) == 0:
        return None

    likes = numbers[0]

    views = (
        numbers[1]
        if len(numbers) >= 2
        else 0
    )

    return {
        "title": title,
        "like": likes,
        "views": views,
    }


# ============================================================
# プロフィールから全プロジェクトを取得
# ============================================================

def scrape_profile(
    page,
    profile_url
):

    print()
    print(
        "=" * 70
    )

    print(
        "プロフィール:",
        profile_url
    )

    print(
        "=" * 70
    )

    try:

        page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(
            PROFILE_PAGE_WAIT
        )

        # ----------------------------------------------------
        # ユーザー名
        # ----------------------------------------------------

        username = (
            get_profile_username(page)
        )

        print(
            "ユーザー名:",
            username
        )

        # ----------------------------------------------------
        # プロジェクトコンテナ
        # ----------------------------------------------------

        container = page.locator(
            "div.bubble-element.Group.baTcwaH1"
        ).first

        # ----------------------------------------------------
        # 現在のHTML構造で見つからない場合、
        # clickable-elementを全体から探す
        # ----------------------------------------------------

        if container.count() > 0:

            cards = container.locator(
                "div.clickable-element"
            ).all()

        else:

            cards = page.locator(
                "div.clickable-element"
            ).all()

        print(
            "カード数:",
            len(cards)
        )

        results = []

        for index, card in enumerate(cards):

            try:

                text = card.inner_text()

                parsed = parse_card(
                    text,
                    username
                )

                if not parsed:
                    continue

                # ------------------------------------------------
                # カードのURLを取得
                # ------------------------------------------------

                card_url = None

                # aタグ
                try:

                    link = card.locator(
                        "a[href]"
                    ).first

                    if link.count() > 0:

                        card_url = (
                            link.get_attribute(
                                "href"
                            )
                        )

                except Exception:
                    pass

                # clickable自身
                if not card_url:

                    try:

                        card_url = (
                            card.get_attribute(
                                "href"
                            )
                        )

                    except Exception:
                        pass

                # ------------------------------------------------
                # onclick等から探す
                # ------------------------------------------------

                if not card_url:

                    try:

                        outer_html = card.evaluate(
                            "(el) => el.outerHTML"
                        )

                        matches = re.findall(
                            r'https://chanpro\.jp/[^"\']*program-profile[^"\']*',
                            outer_html
                        )

                        if matches:
                            card_url = matches[0]

                    except Exception:
                        pass

                card_url = normalize_url(
                    card_url
                )

                parsed["url"] = card_url

                results.append(
                    parsed
                )

                print(
                    f"[{index + 1}] "
                    f"{parsed['title']} "
                    f"いいね={parsed['like']} "
                    f"閲覧={parsed['views']} "
                    f"URL={card_url}"
                )

            except Exception as e:

                print(
                    f"カード {index + 1} "
                    f"取得エラー: {e}"
                )

        return {
            "username": username,
            "projects": results,
            "status": "success",
        }

    except Exception as e:

        print(
            "プロフィール取得失敗:",
            str(e)
        )

        return {
            "username": None,
            "projects": [],
            "status": "error",
            "error": str(e),
        }


# ============================================================
# DBから全ユーザー取得
# ============================================================

def get_all_users(conn):

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT
                id,
                username,
                profile,
                program_urls,
                program_likes
            FROM public.users
            ORDER BY created_at ASC
            """
        )

        rows = cur.fetchall()

    users = []

    for row in rows:

        users.append({
            "id": row[0],
            "username": row[1],
            "profile": row[2],
            "program_urls": row[3] or [],
            "program_likes": row[4] or [],
        })

    return users


# ============================================================
# 前回いいね数をURLから検索
# ============================================================

def get_old_like(
    program_urls,
    program_likes,
    target_url
):

    target_url = normalize_url(
        target_url
    )

    for index, url in enumerate(
        program_urls
    ):

        url = normalize_url(url)

        if url == target_url:

            if index < len(
                program_likes
            ):

                return safe_int(
                    program_likes[index]
                )

            return 0

    return 0


# ============================================================
# notificationsへ追加
# ============================================================

def create_notification(
    conn,
    user_id,
    program_url,
    program_title,
    old_likes,
    new_likes
):

    """
    notificationsテーブルへ通知を作成。

    actor_id:
        NULL
        → 「誰が押したか」ではなく、
          外部プロフィールのいいね数増加を
          検知した通知だから。

    post_id:
        NULL
        → program_urlはposts.idではないため。

    message_id:
        NULL
    """

    with conn.cursor() as cur:

        cur.execute(
            """
            INSERT INTO public.notifications
            (
                user_id,
                actor_id,
                type,
                post_id,
                created_at,
                message_id,
                is_read
            )
            VALUES
            (
                %s,
                NULL,
                'project',
                NULL,
                now(),
                NULL,
                false
            )
            """,
            (
                user_id,
            )
        )

    difference = (
        new_likes - old_likes
    )

    print()
    print(
        "★ 通知作成"
    )

    print(
        "  ユーザー:",
        user_id
    )

    print(
        "  プロジェクト:",
        program_title
    )

    print(
        "  いいね:",
        f"{old_likes} → {new_likes}"
    )

    print(
        "  増加:",
        f"+{difference}"
    )

    return True


# ============================================================
# program_likesを更新
# ============================================================

def update_program_likes(
    conn,
    user_id,
    program_urls,
    current_likes
):

    """
    program_urlsの順番に合わせて
    program_likesを作り直す。

    取得できなかったURLについては
    既存値を維持する。
    """

    new_likes = []

    for url in program_urls:

        normalized = normalize_url(
            url
        )

        # ----------------------------------------------------
        # 今回取得できた
        # ----------------------------------------------------

        if normalized in current_likes:

            new_likes.append(
                current_likes[
                    normalized
                ]
            )

            continue

        # ----------------------------------------------------
        # 今回取得できなかった
        #
        # 既存値を維持
        # ----------------------------------------------------

        old_index = None

        for i, old_url in enumerate(
            program_urls
        ):

            if normalize_url(
                old_url
            ) == normalized:

                old_index = i
                break

        if (
            old_index is not None
            and old_index >= 0
        ):

            # DBからの既存値は別途必要になるので
            # この関数では呼び出し側で処理する。
            new_likes.append(None)

        else:

            new_likes.append(0)

    # Noneを既存値に戻す
    # --------------------------------------------------------

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT program_likes
            FROM public.users
            WHERE id = %s
            """,
            (
                user_id,
            )
        )

        row = cur.fetchone()

        old_likes = (
            row[0] or []
            if row
            else []
        )

        for i in range(
            len(new_likes)
        ):

            if new_likes[i] is None:

                if i < len(old_likes):

                    new_likes[i] = (
                        safe_int(
                            old_likes[i]
                        )
                    )

                else:

                    new_likes[i] = 0

        cur.execute(
            """
            UPDATE public.users
            SET program_likes = %s
            WHERE id = %s
            """,
            (
                new_likes,
                user_id
            )
        )


# ============================================================
# 1ユーザー処理
# ============================================================

def process_user(
    conn,
    page,
    user
):

    user_id = user["id"]
    username = user["username"]

    print()
    print()
    print(
        "#" * 70
    )

    print(
        "ユーザー:",
        username
    )

    print(
        "ID:",
        user_id
    )

    print(
        "#" * 70
    )

    # --------------------------------------------------------
    # プロフィールURL取得
    # --------------------------------------------------------

    profile_url = (
        get_user_profile_url(
            page,
            user
        )
    )

    if not profile_url:

        print(
            "プロフィールURLを取得できませんでした"
        )

        return {
            "user_id": str(user_id),
            "username": username,
            "profile_url": None,
            "status": "profile_url_not_found",
            "projects": [],
            "notifications": [],
        }

    print(
        "プロフィールURL:",
        profile_url
    )

    # --------------------------------------------------------
    # プロフィール取得
    # --------------------------------------------------------

    profile_data = scrape_profile(
        page,
        profile_url
    )

    if profile_data["status"] != "success":

        return {
            "user_id": str(user_id),
            "username": username,
            "profile_url": profile_url,
            "status": "scrape_error",
            "projects": [],
            "notifications": [],
        }

    projects = (
        profile_data["projects"]
    )

    # --------------------------------------------------------
    # DBの前回値
    # --------------------------------------------------------

    program_urls = (
        user["program_urls"]
        or []
    )

    program_likes = (
        user["program_likes"]
        or []
    )

    # --------------------------------------------------------
    # 現在のいいね数
    # --------------------------------------------------------

    current_likes = {}

    # --------------------------------------------------------
    # 通知結果
    # --------------------------------------------------------

    notifications = []

    # --------------------------------------------------------
    # 各プロジェクトを処理
    # --------------------------------------------------------

    for project in projects:

        project_url = normalize_url(
            project.get("url")
        )

        project_title = (
            project.get("title")
            or "プロジェクト"
        )

        new_likes = safe_int(
            project.get("like")
        )

        # ----------------------------------------------------
        # URLが取得できた場合
        # ----------------------------------------------------

        if project_url:

            current_likes[
                project_url
            ] = new_likes

        # ----------------------------------------------------
        # program_urlsから一致するものを探す
        # ----------------------------------------------------

        old_likes = None

        if project_url:

            old_likes = get_old_like(
                program_urls,
                program_likes,
                project_url
            )

        # ----------------------------------------------------
        # URLがカードから取得できなかった場合
        #
        # program_urlsのタイトルとの直接照合はできないので、
        # 順番を利用する。
        # ----------------------------------------------------

        if old_likes is None:

            project_index = (
                projects.index(project)
            )

            if project_index < len(
                program_likes
            ):

                old_likes = safe_int(
                    program_likes[
                        project_index
                    ]
                )

            else:

                old_likes = 0

        # ----------------------------------------------------
        # 増加チェック
        # ----------------------------------------------------

        print()
        print(
            f"プロジェクト: {project_title}"
        )

        print(
            f"いいね: "
            f"{old_likes} → {new_likes}"
        )

        if new_likes > old_likes:

            difference = (
                new_likes - old_likes
            )

            print(
                f"★★★ "
                f"+{difference} いいね"
            )

            # ------------------------------------------------
            # 通知
            # ------------------------------------------------

            create_notification(
                conn,
                user_id,
                project_url,
                project_title,
                old_likes,
                new_likes
            )

            notifications.append({
                "project_title": project_title,
                "program_url": project_url,
                "old_likes": old_likes,
                "new_likes": new_likes,
                "difference": difference,
            })

        else:

            print(
                "いいね増加なし"
            )

    # --------------------------------------------------------
    # DBのprogram_likes更新
    # --------------------------------------------------------

    update_program_likes(
        conn,
        user_id,
        program_urls,
        current_likes
    )

    # --------------------------------------------------------
    # 結果
    # --------------------------------------------------------

    return {
        "user_id": str(user_id),
        "username": username,
        "profile_url": profile_url,
        "status": "success",
        "projects": projects,
        "notifications": notifications,
    }


# ============================================================
# メイン
# ============================================================

def main():

    fetched_at = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    print()
    print(
        "=" * 70
    )

    print(
        "ChanPro いいね通知システム"
    )

    print(
        "=" * 70
    )

    print(
        "開始:",
        fetched_at
    )

    users_checked = 0
    projects_checked = 0
    notifications_created = 0

    result_users = []

    # ========================================================
    # DB
    # ========================================================

    with psycopg.connect(
        DATABASE_URL
    ) as conn:

        users = get_all_users(
            conn
        )

        print()
        print(
            "ユーザー数:",
            len(users)
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
                    "height": 2000,
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
            # 全ユーザー
            # ------------------------------------------------

            for user in users:

                try:

                    result = process_user(
                        conn,
                        page,
                        user
                    )

                    result_users.append(
                        result
                    )

                    users_checked += 1

                    projects_checked += len(
                        result.get(
                            "projects",
                            []
                        )
                    )

                    notifications_created += len(
                        result.get(
                            "notifications",
                            []
                        )
                    )

                    # ユーザーごとにcommit
                    # 途中で1人失敗しても、
                    # それまでの処理を失わない
                    conn.commit()

                except Exception as e:

                    print()
                    print(
                        "ユーザー処理エラー:",
                        user.get(
                            "username"
                        )
                    )

                    print(
                        str(e)
                    )

                    conn.rollback()

                    result_users.append({
                        "user_id": str(
                            user["id"]
                        ),
                        "username": user[
                            "username"
                        ],
                        "profile_url": None,
                        "status": "error",
                        "error": str(e),
                        "projects": [],
                        "notifications": [],
                    })

            browser.close()

    # ========================================================
    # JSON
    # ========================================================

    output = {
        "fetched_at": fetched_at,
        "users_checked": users_checked,
        "projects_checked": projects_checked,
        "notifications_created": (
            notifications_created
        ),
        "users": result_users,
    }

    with open(
        RESULT_FILE,
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
    # 結果
    # ========================================================

    print()
    print(
        "=" * 70
    )

    print(
        "処理完了"
    )

    print(
        "=" * 70
    )

    print(
        "ユーザー確認数:",
        users_checked
    )

    print(
        "プロジェクト確認数:",
        projects_checked
    )

    print(
        "作成通知数:",
        notifications_created
    )

    print(
        "結果:",
        RESULT_FILE
    )

    print(
        "=" * 70
    )


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":
    main()
