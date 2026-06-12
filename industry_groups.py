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
    "ETF": {
        "stocks": {
            "0050":  "元大台灣50",
            "0056":  "元大高股息",
            "00878": "國泰永續高股息",
        },
        "config_overrides": {
            "scoring": {
                "fundamental": {
                    # ETF 無營收毛利等基本面，將門檻降至極低避免影響評分
                    "gross_margin_threshold": -999,
                    "op_margin_threshold": -999,
                    "rev_yoy_growth_score": 0,
                    "rev_yoy_drop_score": 0,
                    "eps_yoy_growth_score": 0,
                    "eps_yoy_drop_score": 0,
                },
                "tech": {
                    # ETF 走勢穩健，新高天數判斷可拉長
                    "price_new_high_days": 120,
                    "ma_bullish_score": 15,
                    "ma_bearish_score": -20,
                },
                "institutional": {
                    # 外資投信動向對權值 ETF 影響很大，略微調升
                    "foreign_buy_score": 15,
                    "it_buy_score": 15,
                }
            }
        }
    },

    "Leveraged_ETF": {
        "stocks": {
            "00631L": "元大台灣50正2",
        },
        "config_overrides": {
            "scoring": {
                "fundamental": {
                    "gross_margin_threshold": -999,
                    "op_margin_threshold": -999,
                    "rev_yoy_growth_score": 0,
                    "rev_yoy_drop_score": 0,
                    "eps_yoy_growth_score": 0,
                    "eps_yoy_drop_score": 0,
                    "gross_margin_score": 0,
                    "op_margin_score": 0
                },
                "chip": {
                    "tdcc_high_score": 0,
                    "tdcc_mid_score": 0,
                    "tdcc_low_score": 0
                },
                "margin": {
                    "margin_drop_score": 0,
                    "margin_surge_score": 0,
                    "short_ratio_score": 0
                },
                "tech": {
                    "ma_bullish_score": 30,
                    "ma_bearish_score": -30,
                    "macd_cross_score": 15,
                    "momentum_5d_score": 15,
                    "momentum_20d_score": 20
                },
                "institutional": {
                    "foreign_buy_score": 20,
                    "foreign_sell_score": -15
                }
            }
        }
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
