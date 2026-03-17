# ============================================================
# industry_groups.py
# 族群設定檔 — 定義各分析族群與其成員股票，以及產業別門檻覆蓋
#
# 格式：
#   {
#     "族群名稱": {
#       "stocks":          { "股票代號": "股票名稱", ... },
#       "config_overrides": { ... }   # 選填，覆蓋 config.json 中的對應值
#     },
#     ...
#   }
#
# config_overrides 支援的結構與 config.json 的 scoring / indicators 相同：
#   "config_overrides": {
#       "scoring": {
#           "fundamental": { "gross_margin_threshold": 40, ... },
#           "tech":         { "price_new_high_days": 90, ... }
#       }
#   }
# ============================================================

INDUSTRY_GROUPS = {
    "持股清單": {
        "stocks": {
            "0050":  "元大台灣50",
            "00878": "國泰永續高股息",
            "2344":  "華邦電",
            "00692": "富邦台50",
            "2880":  "華南金",
            "2330":  "台積電",
        },
        # 持股清單為混合型，使用預設門檻（不設 overrides）
    },

    "AI與半導體": {
        "stocks": {
            "2330": "台積電",
            "2303": "聯電",
            "2454": "聯發科",
            "3231": "緯創",
            "2382": "廣達",
            "2376": "技嘉",
        },
        "config_overrides": {
            "scoring": {
                "fundamental": {
                    # 半導體設計/晶圓代工毛利率普遍偏高，門檻提升才有鑑別力
                    "gross_margin_threshold": 40,
                    "op_margin_threshold":    15,
                },
                "tech": {
                    # AI族群因輪動快，使用稍短的新高週期
                    "price_new_high_days": 45,
                }
            }
        }
    },

    "記憶體": {
        "stocks": {
            "2408": "南亞科",
            "2344": "華邦電",
            "2337": "旺宏",
            "8299": "群聯",
            "3260": "威剛",
            "8271": "宇瞻",
        },
        "config_overrides": {
            "scoring": {
                "fundamental": {
                    # DRAM/NAND 景氣循環股：谷底毛利率偏低，門檻要降才不會誤判
                    "gross_margin_threshold": 15,
                    "op_margin_threshold":     5,
                    # 記憶體復甦的YoY加速意義更大，加重配分
                    "rev_yoy_growth_score":   12,
                },
                "tech": {
                    # 週期性長，用更長的突破週期才具意義
                    "price_new_high_days": 90,
                }
            }
        }
    },
}
