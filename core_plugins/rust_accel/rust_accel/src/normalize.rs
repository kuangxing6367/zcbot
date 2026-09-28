//! OneBot 11 事件归一化 + _est_size 尺寸预算
//! 逐字对齐 core_plugins/onebot_adapter/main.py 的 normalize_event 与
//! framework/core/event_buffer.py EventBuffer._size_of 浅层回退口径：
//! message 段数组只 peek data 字符串值、1MB 重尾截断，顶层跳过 message/raw_message。

use serde_json::{Map, Value};

/// Python len() 语义：字符数（非字节数）——中文等 C2 类文本口径与 Python 逐位一致
#[inline]
fn ccount(s: &str) -> usize {
    s.chars().count()
}

/// 事件体积估算。Python 中同一遍历逻辑既被 _size_of 浅层回退使用（对任意 dict），
/// 也被 normalize_event 用于预算 _est_size（对归一化事件）——两者输入 shape 一致
/// （无 raw_message、含 message 段 / sender 等），因此 Rust 侧一个实现两处复用。
pub fn calc_size(obj: &Value) -> usize {
    let Some(o) = obj.as_object() else {
        return obj.to_string().len() + 64; // 兜底（正常恒为 object）
    };
    let mut total: usize = 64;
    if let Some(Value::String(s)) = o.get("raw_message") {
        total += ccount(s);
    }
    if let Some(m) = o.get("message") {
        if let Some(arr) = m.as_array() {
            for seg in arr {
                if let Some(so) = seg.as_object() {
                    if let Some(Value::Object(d)) = so.get("data") {
                        for dv in d.values() {
                            if let Value::String(s) = dv {
                                total += ccount(s);
                            }
                            if total > (1 << 20) {
                                break;
                            }
                        }
                    } else {
                        total += 24; // at/face 等非文本段经验值
                    }
                } else {
                    total += 24;
                }
                if total > (1 << 20) {
                    break;
                }
            }
        } else if let Some(s) = m.as_str() {
            total += ccount(s);
        } else if let Some(mo) = m.as_object() {
            for sv in mo.values() {
                total += if let Value::String(s) = sv {
                    ccount(s)
                } else {
                    8
                };
            }
        } else {
            total += 64;
        }
    }
    for (k, v) in o.iter() {
        if k == "message" || k == "raw_message" || k == "_est_size" {
            continue;
        }
        total += ccount(k);
        match v {
            Value::String(s) => total += ccount(s),
            Value::Object(vo) => {
                // sender / raw 等嵌套 dict：只数字符串值，不深递归
                for sv in vo.values() {
                    total += if let Value::String(s) = sv {
                        ccount(s)
                    } else {
                        8
                    };
                }
            }
            _ => total += 8,
        }
    }
    total
}

/// OneBot 11 事件 → 框架内部事件（对齐 Python normalize_event）
pub fn normalize_event(raw: &Value, bot_name: &str) -> Option<Map<String, Value>> {
    let obj = raw.as_object()?;
    let post_type = obj.get("post_type").and_then(Value::as_str)?;
    if post_type.is_empty() {
        return None;
    }

    let mut ev = Map::new();
    ev.insert("type".to_string(), Value::String(post_type.to_string()));
    let sub_key = format!("{}_type", post_type);
    ev.insert(
        "sub_type".to_string(),
        obj.get(&sub_key)
            .cloned()
            .unwrap_or_else(|| Value::String(String::new())),
    );
    ev.insert(
        "message_type".to_string(),
        obj.get("message_type")
            .cloned()
            .unwrap_or_else(|| Value::String(String::new())),
    );
    ev.insert(
        "user_id".to_string(),
        obj.get("user_id")
            .cloned()
            .unwrap_or(Value::Number(0.into())),
    );
    ev.insert(
        "group_id".to_string(),
        obj.get("group_id").cloned().unwrap_or(Value::Null),
    );
    ev.insert(
        "message_id".to_string(),
        obj.get("message_id").cloned().unwrap_or(Value::Null),
    );
    ev.insert(
        "message".to_string(),
        obj.get("message")
            .cloned()
            .unwrap_or_else(|| Value::String(String::new())),
    );
    ev.insert(
        "sender".to_string(),
        obj.get("sender")
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new())),
    );
    ev.insert("bot_name".to_string(), Value::String(bot_name.to_string()));
    ev.insert("adapter".to_string(), Value::String("onebot".to_string()));
    ev.insert("raw".to_string(), raw.clone());

    let sz = calc_size(&Value::Object(ev.clone()));
    ev.insert("_est_size".to_string(), Value::Number(sz.into()));
    Some(ev)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// 2026-09-28 由 Python 真实实现对拍生成：
    /// normalize_event(raw).['_est_size'] = 270；EventBuffer._size_of 同 = 270
    const GOLDEN_EST_SIZE: u64 = 270;

    fn golden_raw() -> Value {
        json!({
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "group_id": 123,
            "user_id": 456,
            "message_id": 9,
            "message": [{"type": "text", "data": {"text": "hello world"}}],
            "sender": {"user_id": 456, "nickname": "alice"}
        })
    }

    #[test]
    fn no_post_type_returns_none() {
        assert!(normalize_event(&json!({"message": "hi"}), "bot").is_none());
    }

    #[test]
    fn empty_post_type_returns_none() {
        assert!(normalize_event(&json!({"post_type": ""}), "bot").is_none());
    }

    #[test]
    fn non_object_returns_none() {
        assert!(normalize_event(&json!([1, 2]), "bot").is_none());
        assert!(normalize_event(&Value::Null, "bot").is_none());
    }

    #[test]
    fn text_segment_normalize_matches_python() {
        let ev = normalize_event(&golden_raw(), "bot1").expect("normalized");
        assert_eq!(ev["type"], "message");
        // Python 语义：sub_type = raw[{post_type}_type] = message_type 的值（raw 的 "sub_type" 被忽略）
        assert_eq!(ev["sub_type"], "group");
        assert_eq!(ev["message_type"], "group");
        assert_eq!(ev["user_id"], 456);
        assert_eq!(ev["group_id"], 123);
        assert_eq!(ev["message_id"], 9);
        assert_eq!(ev["bot_name"], "bot1");
        assert_eq!(ev["adapter"], "onebot");
        assert_eq!(ev["message"], golden_raw()["message"]);
        assert_eq!(ev["raw"], golden_raw());
        // 黄金对拍：与 Python normalize_event / _size_of 输出逐位一致
        assert_eq!(ev["_est_size"].as_u64().unwrap(), GOLDEN_EST_SIZE);
        // 快路径契约：calc_size(归一化事件) 即 _est_size（Python 入队 O(1) 取值）
        assert_eq!(
            calc_size(&Value::Object(ev.clone())) as u64,
            GOLDEN_EST_SIZE
        );
    }

    #[test]
    fn chinese_text_uses_char_count() {
        // 与 Python 对拍（2026-09-28 真实实现）：delta = 12
        // = 4（"你好世界" 按字符计，Python len 语义）+ 8（normalize 携带的 raw.message
        //   数组在浅层回退中按非字符串 +8）。
        // 若误按字节计（UTF-8 12 字节）则 delta = 12+8 = 20，可区分。
        let base =
            json!({"post_type": "message", "message_type": "group", "group_id": 1, "user_id": 2});
        let with_cn = normalize_event(
            &json!({"post_type": "message", "message_type": "group", "group_id": 1, "user_id": 2,
                    "message": [{"type": "text", "data": {"text": "你好世界"}}]}),
            "b",
        )
        .unwrap()
        .get("_est_size")
        .unwrap()
        .as_u64()
        .unwrap();
        let no_msg = normalize_event(&base, "b")
            .unwrap()
            .get("_est_size")
            .unwrap()
            .as_u64()
            .unwrap();
        assert_eq!(
            with_cn - no_msg,
            12,
            "中文按字符数与 Python 一致应为 12（4 字符 + raw.message 骨架 8）"
        );
    }

    #[test]
    fn base64_image_tail_truncated() {
        // 与 Python 对拍（2026-09-28 真实实现）：
        // - 单段超大 base64：先完整累加再 break（Python 与 Rust 同），长度不同则 _est_size 不同
        //   （1200KB → 1229029；99MB → 103809253），不连同段断点累计。
        // - 多段累计超 1MB：在超限段处 break，其后段不再计入
        //   （两段 900KB → 1843429：64 + 921600x2 前段已加 + 顶层 165 = 断在第二段加完）。
        let mk = |size_list: Vec<usize>| {
            let segs: Vec<Value> = size_list
                .iter()
                .map(|sz| json!({"type": "image", "data": {"file": "A".repeat(*sz)}}))
                .collect();
            json!({
                "post_type": "message", "message_type": "group", "group_id": 1, "user_id": 2,
                "message": segs,
                "sender": {}
            })
        };
        let single_big = normalize_event(&mk(vec![1200 * 1024]), "b").unwrap();
        assert_eq!(
            single_big["_est_size"].as_u64().unwrap(),
            1229029,
            "1200KB 单段与 Python 对拍值一致"
        );
        let huge = normalize_event(&mk(vec![99 * 1024 * 1024]), "b").unwrap();
        assert_eq!(
            huge["_est_size"].as_u64().unwrap(),
            103809253,
            "99MB 单段与 Python 对拍值一致（完整累加后 break）"
        );
        // 多段累计截断：断言严格小于"两段全量"（1843200+229）
        let multi = normalize_event(&mk(vec![900 * 1024, 900 * 1024]), "b").unwrap();
        assert_eq!(
            multi["_est_size"].as_u64().unwrap(),
            1843429,
            "两段 900KB 与 Python 对拍值一致（超 1MB 即截断）"
        );
        assert!(
            multi["_est_size"].as_u64().unwrap() < (2 << 20),
            "多段累计截断后不应超过 2MB"
        );
    }

    #[test]
    fn notice_and_meta_types() {
        let n = normalize_event(
            &json!({"post_type": "notice", "notice_type": "group_increase", "group_id": 1, "user_id": 2}),
            "b",
        )
        .unwrap();
        assert_eq!(n["type"], "notice");
        // Python 语义：sub_type = raw["notice_type"]
        assert_eq!(n["sub_type"], "group_increase");
        let m = normalize_event(
            &json!({"post_type": "meta_event", "meta_event_type": "heartbeat"}),
            "b",
        )
        .unwrap();
        assert_eq!(m["type"], "meta_event");
        assert_eq!(m["sub_type"], "heartbeat");
    }

    #[test]
    fn missing_common_fields_defaults() {
        let ev = normalize_event(&json!({"post_type": "message"}), "b").unwrap();
        assert_eq!(ev["user_id"], 0);
        assert_eq!(ev["group_id"], Value::Null);
        assert_eq!(ev["message"], "");
        assert_eq!(ev["sender"], Value::Object(Map::new()));
    }
}
