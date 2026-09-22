import os
import sys
import json
import csv
import io
import re
import time
from datetime import datetime, timezone, timedelta
import urllib.request

JST = timezone(timedelta(hours=9))
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now(JST).strftime("%Y-%m-%d")
CAPTURED_AT = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

print(f"=== クラウド自動実行開始: 日付[{TARGET_DATE}], 取得時刻[{CAPTURED_AT}] ===")

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
history_rows = []

# 全レースを処理
for idx, r in enumerate(races, start=1):
    raw_race_id = r.get("race_id", "")
    parts = raw_race_id.split("_")
    n_id = f"{parts[0]}{parts[2]}{parts[3]}{parts[4]}{parts[5]}" if len(parts) > 5 else raw_race_id
    race_num = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else idx
    r_name, nk_horses = fetch_netkeiba_details(n_id)
    time.sleep(0.2)

    odds_snapshot = r.get("odds_snapshot_at", "不明")
    dist = r.get("distance_m", "")
    surface = r.get("surface", "")
    going_info = r.get("going", "")
    baba_str = f"{surface}_{going_info}" if surface or going_info else "不明"

    sample_horse = r.get("horses", [{}])[0]
    p_pred = sample_horse.get("pace_shape_predicted", {}).get("predicted_curve", {})
    pace_probs = p_pred.get("pace_probs", {})
    h_prob = round(pace_probs.get("H", 0) * 100, 1)
    s_prob = round(pace_probs.get("S", 0) * 100, 1)
    pressure = round(p_pred.get("pressure_score", 0), 2)

    l_dict = {item.get("horse_no"): item for item in late_races.get(raw_race_id, [])}
    for h in r.get("horses", []):
        no = h.get("horse_no")
        lh = l_dict.get(no, {})
        nkh = nk_horses.get(no, {})

        m_share = lh.get("model_share", h.get("model_share", 0)) or 0
        mk_share = lh.get("market_share", h.get("market_share", 0)) or 0
        div = lh.get("divergence", h.get("divergence", 0)) or 0
        odds = lh.get("win_odds_t10", h.get("win_odds_t10", 0)) or 0
        place_odds = lh.get("place_odds_t10", h.get("place_odds_t10", ""))
        place_prob = h.get("pl_place_prob", 0) or 0
        ev = round(m_share * odds, 2) if odds else 0
        bw_score = lh.get("bw_response_score", h.get("bw_response_score", 0))

        csv_rows.append({
            "取得日時": CAPTURED_AT,
            "オッズ基準日時": odds_snapshot,
            "日付": TARGET_DATE,
            "場名": r.get("venue"),
            "R": race_num,
            "レース名": r_name or r.get("race_name") or f"第{race_num}競走",
            "元レースID": raw_race_id,
            "発走": r.get("post_time"),
            "距離": dist,
            "馬場": baba_str,
            "Hペース率(%)": h_prob,
            "Sペース率(%)": s_prob,
            "先行争い圧力": pressure,
            "枠": nkh.get("枠"),
            "馬番": no,
            "馬名": h.get("name"),
            "性齢": h.get("sex_age"),
            "斤量": h.get("carried_weight"),
            "騎手": nkh.get("騎手"),
            "調教師": nkh.get("調教師"),
            "人気": lh.get("popularity", h.get("popularity")),
            "単勝オッズ": odds,
            "複勝オッズ帯": place_odds,
            "AI能力順位": h.get("strength_rank"),
            "AI勝率(%)": round(m_share * 100, 2),
            "市場勝率(%)": round(mk_share * 100, 2),
            "妙味度(歪み)": round(div * 100, 3),
            "期待値(EV)": ev,
            "複勝率(%)": round(place_prob * 100, 2),
            "脚質": h.get("style_label"),
            "位置取り指数": h.get("style_early_position"),
            "馬体重": lh.get("bodyweight_kg", h.get("bodyweight_kg")),
            "増減": lh.get("bodyweight_delta_kg", h.get("bodyweight_delta_kg")),
            "体重評価スコア": round(bw_score, 4) if bw_score is not None else 0,
        })

        for past in h.get("past_runs", []):
            history_rows.append({
                "元レースID": raw_race_id,
                "馬番": no,
                "馬名": h.get("name"),
                "出走日": past.get("date"),
                "距離": past.get("distance_m"),
                "馬場": past.get("surface"),
                "着順": past.get("finish_order"),
                "頭数": past.get("field_size"),
                "上がり3F": past.get("late_3f_sec"),
                "上がり優位度": past.get("late_3f_advantage"),
                "通過順比率": past.get("corner_ratio"),
            })

# CSV文字列の作成
def build_csv_str(rows):
    if not rows:
        return ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()

main_csv_content = build_csv_str(csv_rows)
hist_csv_content = build_csv_str(history_rows)

main_filename = f"keiba_integrated_{TARGET_DATE}.csv"
hist_filename = f"horse_history_{TARGET_DATE}.csv"

# 3. GASを経由して2ファイルを同時にGoogleドライブへ送信
gas_url = os.environ.get("GAS_WEBHOOK_URL")
if not gas_url:
    print("× エラー: GAS_WEBHOOK_URL が設定されていません")
    sys.exit(1)

payload = {
    "files": [
        {"filename": main_filename, "content": main_csv_content},
        {"filename": hist_filename, "content": hist_csv_content}
    ]
}

req_gas = urllib.request.Request(
    gas_url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}
)

try:
    with urllib.request.urlopen(req_gas) as res:
        print(f"○ Googleドライブへの保存完了: {main_filename}, {hist_filename}")
except Exception as e:
    print(f"× 送信エラー: {e}")
    sys.exit(1)
