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

    # レース共通の環境・ペース指標
    dist = r.get("distance_m", "")
    surface = r.get("surface", "")
    going = r.get("going", "")
    
    # ペース予測データの抽出
    sample_horse = r.get("horses", [{}])[0]
    p_pred = sample_horse.get("pace_shape_predicted", {}).get("predicted_curve", {})
    pace_probs = p_pred.get("pace_probs", {})
    h_prob = round(pace_probs.get("H", 0) * 100, 1)
    m_prob = round(pace_probs.get("M", 0) * 100, 1)
    s_prob = round(pace_probs.get("S", 0) * 100, 1)
    pressure = round(p_pred.get("pressure_score", 0), 2)

    l_dict = {item.get("horse_no"): item for item in late_races.get(tid, [])}
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

        # 馬体重評価スコア
        bw_score = lh.get("bw_response_score", h.get("bw_response_score", 0))

        csv_rows.append({
            "日付": TARGET_DATE,
            "場名": r.get("venue"),
            "R": race_num,
            "レース名": r_name or r.get("race_name") or f"第{race_num}競走",
            "発走": r.get("post_time"),
            "距離": dist,
            "馬場": f"{surface}_{going}",
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

output = io.StringIO()
writer = csv.DictWriter(output, fieldnames=list(csv_rows[0].keys()))
writer.writeheader()
writer.writerows(csv_rows)
csv_content = output.getvalue()

csv_file_name = f"keiba_integrated_{TARGET_DATE}.csv"

# 3. GASを経由してGoogleドライブへ送信
gas_url = os.environ.get("GAS_WEBHOOK_URL")
if not gas_url:
    print("× エラー: GAS_WEBHOOK_URL が設定されていません")
    sys.exit(1)

payload = {
    "filename": csv_file_name,
    "content": csv_content
}

req_gas = urllib.request.Request(
    gas_url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}
)

try:
    with urllib.request.urlopen(req_gas) as res:
        print(f"○ Googleドライブへの保存完了: {csv_file_name}")
except Exception as e:
    print(f"× 送信エラー: {e}")
    sys.exit(1)
