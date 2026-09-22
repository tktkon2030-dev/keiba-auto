import os
import sys
import json
import csv
import re
import time
from datetime import datetime, timezone, timedelta
import urllib.request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 日本時間(JST)で当日日付を取得
JST = timezone(timedelta(hours=9))
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now(JST).strftime("%Y-%m-%d")

print(f"=== クラウド自動実行開始: [{TARGET_DATE}] ===")

# 1. 友人ツールデータ取得
tool_url = f"https://keiba-simu-euphoria.ef4-db.workers.dev/days/{TARGET_DATE}.json"
req = urllib.request.Request(tool_url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req) as res:
        raw_data = json.loads(res.read().decode("utf-8"))
except Exception as e:
    print(f"データ取得なし/エラー: {e}")
    sys.exit(0)

races = raw_data.get("races", [])
late_races = raw_data.get("late", {}).get("races", {})

# 2. netkeiba詳細取得
def fetch_netkeiba_details(netkeiba_id):
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={netkeiba_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req) as res:
            html = res.read().decode("utf-8", errors="replace")
    except Exception:
        return "", {}

    name_m = re.search(r'<h1 class="RaceName">([^<]+)</h1>', html)
    race_name = name_m.group(1).strip() if name_m else ""
    horses = {}
    for row in re.findall(r'<tr[^>]*class="[^"]*HorseList[^"]*"[^>]*>(.*?)</tr>', html, re.DOTALL):
        u_m = re.search(r'<td[^>]*class="[^"]*Umaban[^"]*"[^>]*>\s*(\d+)\s*</td>', row)
        w_m = re.search(r'<td[^>]*class="[^"]*Waku[^"]*"[^>]*>.*?<span>(\d+)</span>', row, re.DOTALL)
        j_m = re.search(r'<td[^>]*class="[^"]*Jockey[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>', row, re.DOTALL)
        t_m = re.search(r'<td[^>]*class="[^"]*Trainer[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>', row, re.DOTALL)
        if u_m:
            horses[int(u_m.group(1))] = {
                "枠": int(w_m.group(1)) if w_m else "-",
                "騎手": j_m.group(1).strip() if j_m else "不明",
                "調教師": t_m.group(1).strip() if t_m else "不明"
            }
    return race_name, horses

csv_rows = []
for idx, r in enumerate(races, start=1):
    tid = r.get("race_id", "")
    parts = tid.split("_")
    n_id = f"{parts[0]}{parts[2]}{parts[3]}{parts[4]}{parts[5]}"
    race_num = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else idx
    r_name, nk_horses = fetch_netkeiba_details(n_id)
    time.sleep(0.3)

    l_dict = {item.get("horse_no"): item for item in late_races.get(tid, [])}
    for h in r.get("horses", []):
        no = h.get("horse_no")
        lh = l_dict.get(no, {})
        nkh = nk_horses.get(no, {})
        m_share = lh.get("model_share", h.get("model_share", 0)) or 0
        mk_share = lh.get("market_share", h.get("market_share", 0)) or 0
        div = lh.get("divergence", h.get("divergence", 0)) or 0
        odds = lh.get("win_odds_t10", h.get("win_odds_t10", 0)) or 0
        place_prob = h.get("pl_place_prob", 0) or 0
        ev = round(m_share * odds, 2) if odds else 0

        csv_rows.append({
            "日付": TARGET_DATE,
            "場名": r.get("venue"),
            "R": race_num,
            "レース名": r_name or r.get("race_name") or f"第{race_num}競走",
            "発走": r.get("post_time"),
            "枠": nkh.get("枠"),
            "馬番": no,
            "馬名": h.get("name"),
            "騎手": nkh.get("騎手"),
            "調教師": nkh.get("調教師"),
            "人気": h.get("popularity"),
            "単勝オッズ": odds,
            "AI勝率(%)": round(m_share * 100, 2),
            "市場勝率(%)": round(mk_share * 100, 2),
            "妙味度(歪み)": round(div * 100, 3),
            "期待値(EV)": ev,
            "複勝率(%)": round(place_prob * 100, 2),
            "脚質": h.get("style_label"),
            "馬体重": h.get("bodyweight_kg"),
            "増減": h.get("bodyweight_delta_kg"),
        })

csv_file_name = f"keiba_integrated_{TARGET_DATE}.csv"
with open(csv_file_name, mode="w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
    writer.writeheader()
    writer.writerows(csv_rows)

# 3. Googleドライブへ直接アップロード
sa_info = json.loads(os.environ["GDRIVE_SA_KEY"])
folder_id = os.environ["GDRIVE_FOLDER_ID"]
creds = service_account.Credentials.from_service_account_info(
    sa_info, scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build("drive", "v3", credentials=creds)

# 既存の同名ファイルがあれば削除
query = f"name = '{csv_file_name}' and '{folder_id}' in parents and trashed = false"
existing = drive_service.files().list(q=query, fields="files(id)").execute().get("files", [])
for file in existing:
    drive_service.files().delete(fileId=file["id"]).execute()

media = MediaFileUpload(csv_file_name, mimetype="text/csv")
file_metadata = {"name": csv_file_name, "parents": [folder_id]}
drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()
print(f"○ Googleドライブへのアップロードが完了しました: {csv_file_name}")
