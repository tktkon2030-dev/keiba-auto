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
now_jst = datetime.now(JST)
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else now_jst.strftime("%Y-%m-%d")
CAPTURED_AT_STR = now_jst.strftime("%Y-%m-%d %H:%M:%S")

print(f"=== クラウド自動実行開始: 日付[{TARGET_DATE}], 取得時刻[{CAPTURED_AT_STR}] ===")

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

for idx, r in enumerate(races, start=1):
    raw_race_id = r.get("race_id", "")
    parts = raw_race_id.split("_")
    n_id = f"{parts[0]}{parts[2]}{parts[3]}{parts[4]}{parts[5]}" if len(parts) > 5 else raw_race_id
    race_num = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else idx
    r_name, nk_horses = fetch_netkeiba_details(n_id)
    time.sleep(0.2)

    post_time_str = r.get("post_time", "")
    odds_snapshot_str = r.get("odds_snapshot_at")

    # 厳密な時刻検査
    is_pre_race = 0
    odds_elapsed_min = "不明"
    try:
        if post_time_str and odds_snapshot_str:
            # ISOフォーマット等のパース
            odds_clean = odds_snapshot_str.replace("Z", "+00:00")
            odds_dt = datetime.fromisoformat(odds_clean).astimezone(JST)
            post_dt = datetime.strptime(f"{TARGET_DATE} {post_time_str}:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)

            if odds_dt <= now_jst and now_jst < post_dt:
                is_pre_race = 1

            # オッズ基準時刻からの経過時間（分）
            elapsed = (now_jst - odds_dt).total_seconds() / 60.0
            odds_elapsed_min = round(elapsed, 1)
    except Exception:
        pass

    dist = r.get("distance_m", "")
    surface = r.get("surface", "不明")
    going_info = r.get("going", "不明")
    if isinstance(going_info, dict):
        going_info = going_info.get("surface_going") or going_info.get("turf") or going_info.get("dirt") or "不明"

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

        # モデル更新時刻
        model_updated_at = lh.get("_captured_at") or h.get("mark_late", {}).get("_captured_at") or "不明"

        # 損益分岐勝率 (1 / 単勝オッズ)
        be_win_rate = round((1.0 / odds) * 100, 2) if odds and odds > 0 else 0

        csv_rows.append({
            "取得日時": CAPTURED_AT_STR,
            "事前予想有効": is_pre_race,
            "発走予定時刻": post_time_str,
            "オッズ基準日時": odds_snapshot_str or "不明",
            "オッズ経過分": odds_elapsed_min,
            "モデル更新日時": model_updated_at,
            "日付": TARGET_DATE,
            "場名": r.get("venue"),
            "R": race_num,
            "レース名": r_name or r.get("race_name") or f"第{race_num}競走",
            "元レースID": raw_race_id,
            "距離": dist,
            "芝ダ": surface,
            "馬場状態": going_info,
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
            "損益分岐勝率(%)": be_win_rate,
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
            lap_prof = past.get("lap_profile") or {}
            laps_list = lap_prof.get("laps", []) if isinstance(lap_prof, dict) else []
            late_3f = past.get("late_3f_sec")
            late_3f_usable = 1 if (late_3f and late_3f >= 25.0) else 0

            history_rows.append({
                "元レースID": raw_race_id,
                "馬番": no,
                "馬名": h.get("name"),
                "出走日": past.get("date"),
                "距離": past.get("distance_m"),
                "芝ダ": past.get("surface") or "不明",
                "馬場状態": past.get("going") or "不明",
                "着順": past.get("finish_order"),
                "頭数": past.get("field_size"),
                "上がり3F": late_3f,
                "上がり3F暫定使用可": late_3f_usable,
                "上がり優位度": past.get("late_3f_advantage"),
                "通過順比率": past.get("corner_ratio"),
                "実測ラップ配列": json.dumps(laps_list) if laps_list else "[]"
            })

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
