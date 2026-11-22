"""离线测试：不联网，覆盖判定、去重、上限与静默逻辑。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor import cluster, lang, newsnow, rules, run as monitor_run, state as state_mod  # noqa: E402

NOW_MS = 1794945316764


def item(source, platform, title, url, heat=None, ms=NOW_MS):
    return newsnow.HotItem(source=source, platform=platform, label=platform, title=title,
                           url=url, heat=heat, heat_text="", published_ms=None,
                           updated_ms=ms)


class NewsnowParseTests(unittest.TestCase):
    def test_parses_items_and_heat(self):
        payload = {
            "status": "cache", "id": "zhihu", "updatedTime": NOW_MS,
            "items": [
                {"id": "1", "title": "美商务部发布限制远程获取先进半导体相关措施 中方回应",
                 "url": "https://www.zhihu.com/question/1",
                 "extra": {"info": "395 万热度", "hover": "摘要"}},
                {"id": "2", "title": "US stocks rally", "url": "https://e/2", "extra": {}},
            ],
        }
        source = mock.Mock(id="zhihu", platform="zhihu", label="知乎")
        items = newsnow.parse_payload(payload, source)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].heat, 3_950_000)
        self.assertEqual(items[0].updated_ms, NOW_MS)
        self.assertEqual(items[0].summary, "摘要")

    def test_heat_units(self):
        self.assertEqual(newsnow.parse_heat("1.2亿热度"), 120_000_000)
        self.assertEqual(newsnow.parse_heat("409 万热度"), 4_090_000)
        self.assertIsNone(newsnow.parse_heat("热门"))
        self.assertIsNone(newsnow.parse_heat(""))

    def test_missing_fields_raise(self):
        source = mock.Mock(id="x", platform="x", label="x")
        with self.assertRaises(ValueError):
            newsnow.parse_payload({"items": "oops"}, source)
        with self.assertRaises(ValueError):
            newsnow.parse_payload({}, source)

    def test_items_without_title_or_url_dropped(self):
        payload = {"items": [{"title": "", "url": "https://a"}, {"title": "有标题", "url": ""},
                             {"title": "正常", "url": "https://c"}]}
        source = mock.Mock(id="x", platform="x", label="x")
        items = newsnow.parse_payload(payload, source)
        self.assertEqual(len(items), 1)

    def test_pub_date_and_extra_date(self):
        payload = {"items": [{"title": "标题一二三四五六", "url": "https://a",
                              "pubDate": NOW_MS - 1000,
                              "extra": {"date": NOW_MS - 2000}}]}
        source = mock.Mock(id="x", platform="x", label="x")
        items = newsnow.parse_payload(payload, source)
        self.assertEqual(items[0].published_ms, NOW_MS - 1000)


class ChineseFilterTests(unittest.TestCase):
    def test_english_blocked(self):
        self.assertFalse(lang.is_chinese("US stocks rally as Fed signals rate cut"))
        self.assertFalse(lang.is_chinese("Tesla announces new model"))
        self.assertFalse(lang.is_chinese("APEC"))

    def test_chinese_kept(self):
        self.assertTrue(lang.is_chinese("美商务部发布限制远程获取先进半导体相关措施 中方回应"))
        self.assertTrue(lang.is_chinese("小米回应雷军APEC发言紧张"))
        self.assertTrue(lang.is_chinese("特朗普启程亚洲行，准备参与 APEC 会议"))

    def test_ratio_computed(self):
        count, ratio = lang.chinese_stats("美商务部回应")
        self.assertEqual(count, 6)
        self.assertEqual(ratio, 1.0)

    def test_market_quote_filtered(self):
        self.assertTrue(lang.is_market_quote("WTI 12月原油期货收涨0.29美元，涨幅0.34%，报86.01美元/桶"))
        self.assertTrue(lang.is_market_quote("道琼斯指数跌幅扩大至2%"))
        self.assertFalse(lang.is_market_quote("美商务部发布限制远程获取先进半导体相关措施"))

    def test_non_news_topic_filtered(self):
        self.assertTrue(lang.is_non_news_topic("国足2:2战平巴林"))
        self.assertTrue(lang.is_non_news_topic("某演唱会门票开售"))
        self.assertFalse(lang.is_non_news_topic("俄罗斯总统普京抵达深圳"))


class ClusterTests(unittest.TestCase):
    def test_three_platforms_is_hot(self):
        items = [
            item("zhihu", "zhihu", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://a"),
            item("cls-hot", "cls", "美商务部发布限制远程获取先进半导体措施 中方回应", "https://b"),
            item("tencent-hot", "tencent", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://c"),
        ]
        events, near, _ = cluster.cluster(items, NOW_MS)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].platforms), 3)

    def test_two_platforms_not_hot(self):
        items = [
            item("zhihu", "zhihu", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://a"),
            item("cls-hot", "cls", "美商务部发布限制远程获取先进半导体措施 中方回应", "https://b"),
        ]
        events, near, _ = cluster.cluster(items, NOW_MS)
        self.assertEqual(events, [])
        self.assertEqual(len(near), 1)

    def test_same_platform_counts_once(self):
        items = [
            item("cls-telegraph", "cls", "浑水做空英伟达，机构下调目标价", "https://a"),
            item("cls-hot", "cls", "浑水做空英伟达 机构下调目标价", "https://b"),
            item("cls-depth", "cls", "浑水做空英伟达", "https://c"),
        ]
        events, near, _ = cluster.cluster(items, NOW_MS)
        self.assertEqual(events, [])
        self.assertEqual(len(near), 0)

    def test_proper_noun_clustering(self):
        left = lang.build("特朗普启程亚洲行，准备参与 APEC 会议")
        right = lang.build("特朗普启程亚洲行 将参加 APEC 会议")
        other = lang.build("小米回应雷军APEC发言紧张")
        self.assertTrue(lang.same_event(left, right))
        self.assertFalse(lang.same_event(left, other))
        self.assertTrue(lang.similarity(left, right).anchor)

    def test_generic_phrase_alone_is_not_anchor(self):
        left = lang.build("某地暴雨")
        right = lang.build("另一地暴雨")
        self.assertFalse(lang.has_anchor(lang.similarity(left, right)))

    def test_common_english_token_is_not_anchor(self):
        left = lang.build("OpenAI 推出新模型")
        right = lang.build("英伟达 AI 芯片")
        match = lang.similarity(left, right)
        self.assertFalse(len(match.shared_ascii) >= 2)
        self.assertFalse(lang.same_event(left, right))

    def test_english_items_dropped(self):
        items = [
            item("a", "a", "Fed signals rate cut in December", "https://a"),
            item("b", "b", "Fed signals rate cut in December", "https://b"),
            item("c", "c", "Fed signals rate cut in December", "https://c"),
        ]
        events, near, stats = cluster.cluster(items, NOW_MS)
        self.assertEqual(events, [])
        self.assertEqual(stats["not_chinese"], 3)

    def test_stale_items_dropped(self):
        old = NOW_MS - 40 * 3600 * 1000
        items = [
            item("a", "a", "美商务部发布限制措施 中方回应", "https://a", ms=old),
            item("b", "b", "美商务部发布限制措施 中方回应", "https://b", ms=old),
            item("c", "c", "美商务部发布限制措施 中方回应", "https://c", ms=old),
        ]
        events, near, stats = cluster.cluster(items, NOW_MS)
        self.assertEqual(events, [])
        self.assertEqual(stats["stale"], 3)

    def test_market_quote_and_sports_dropped(self):
        items = [
            item("a", "a", "WTI 12月原油期货收涨0.29美元，涨幅0.34%，报86.01美元/桶", "https://a"),
            item("b", "b", "WTI 12月原油期货收涨0.29美元，涨幅0.34%，报86.01美元/桶", "https://b"),
            item("c", "c", "WTI 12月原油期货收涨0.29美元，涨幅0.34%，报86.01美元/桶", "https://c"),
            item("d", "d", "国足2:2战平巴林", "https://d"),
            item("e", "e", "国足2:2战平巴林", "https://e"),
            item("f", "f", "国足2:2战平巴林", "https://f"),
        ]
        events, near, stats = cluster.cluster(items, NOW_MS)
        self.assertEqual(events, [])
        self.assertEqual(stats["market_quote"], 3)
        self.assertEqual(stats["non_news"], 3)

    def test_ten_item_cap(self):
        items = []
        for i in range(15):
            chars = "".join(chr(0x4E00 + i * 8 + offset) for offset in range(4))
            for platform in ("zhihu", "cls", "tencent"):
                items.append(item(platform, platform, f"{chars}公司{chars}", f"https://x/{i}/{platform}"))
        events, _, _ = cluster.cluster(items, NOW_MS)
        self.assertEqual(len(events), 15)
        self.assertEqual(len(events[:rules.MAX_ITEMS]), 10)

    def test_duplicate_titles_in_one_source_merged(self):
        items = [
            item("zhihu", "zhihu", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://a"),
            item("zhihu", "zhihu", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://a2"),
            item("cls-hot", "cls", "美商务部发布限制远程获取先进半导体措施 中方回应", "https://b"),
            item("tencent-hot", "tencent", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://c"),
        ]
        events, _, stats = cluster.cluster(items, NOW_MS)
        self.assertEqual(len(events), 1)
        self.assertEqual(stats["duplicated"], 1)
        self.assertEqual(len(events[0].items), 3)


class StateTests(unittest.TestCase):
    def test_seen_event_not_pushed_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            st = state_mod.State(path)
            items = [
                item("zhihu", "zhihu", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://a"),
                item("cls-hot", "cls", "美商务部发布限制远程获取先进半导体措施 中方回应", "https://b"),
                item("tencent-hot", "tencent", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://c"),
            ]
            events, _, _ = cluster.cluster(items, NOW_MS)
            event = events[0]
            self.assertFalse(st.known_event(event))
            st.mark_event(event)
            st.save()
            again = state_mod.State(path).load()
            self.assertTrue(again.known_event(event))


class RunTests(unittest.TestCase):
    """用样例与假数据跑编排，覆盖上限、静默、单源失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.report = os.path.join(self.tmp.name, "report.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _fixture(self):
        return {
            "sources": [
                {"source": "zhihu", "platform": "zhihu", "label": "知乎", "updated_ms": NOW_MS,
                 "items": [{"title": "美商务部发布限制远程获取先进半导体相关措施，中方回应",
                            "url": "https://a/1", "heat": 3_950_000}]},
                {"source": "cls-hot", "platform": "cls", "label": "财联社热榜", "updated_ms": NOW_MS,
                 "items": [{"title": "美商务部发布限制远程获取先进半导体措施 中方回应",
                            "url": "https://b/1"}]},
                {"source": "tencent-hot", "platform": "tencent", "label": "腾讯热榜", "updated_ms": NOW_MS,
                 "items": [{"title": "美商务部发布限制远程获取先进半导体相关措施，中方回应",
                            "url": "https://c/1"}]},
            ]
        }

    def _write_fixture(self):
        path = os.path.join(self.tmp.name, "fixture.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self._fixture(), handle, ensure_ascii=False)
        return path

    def test_dry_run_prints_message_and_does_not_mark_seen(self):
        path = self._write_fixture()
        code = monitor_run.run(fixtures=path, state_path=self.state, report_path=self.report,
                               now_ms=NOW_MS)
        self.assertEqual(code, 0)
        report = json.load(open(self.report, encoding="utf-8"))
        self.assertEqual(len(report["selected"]), 1)
        self.assertIn("美商务部", report["message"])
        self.assertFalse(report["telegram_sent"])
        state = json.load(open(self.state, encoding="utf-8"))
        self.assertEqual(state["seen"], [])

    def test_silent_when_no_hot_event(self):
        payload = {"sources": [
            {"source": "zhihu", "platform": "zhihu", "label": "知乎", "updated_ms": NOW_MS,
             "items": [{"title": "英伟达发布新显卡", "url": "https://a/1"}]},
            {"source": "cls-hot", "platform": "cls", "label": "财联社热榜", "updated_ms": NOW_MS,
             "items": [{"title": "小米回应雷军APEC发言紧张", "url": "https://b/1"}]},
        ]}
        path = os.path.join(self.tmp.name, "quiet.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        with mock.patch.object(monitor_run.telegram, "send_message",
                               side_effect=AssertionError("不该推送")):
            code = monitor_run.run(fixtures=path, state_path=self.state, report_path=self.report,
                                   now_ms=NOW_MS)
        self.assertEqual(code, 0)
        report = json.load(open(self.report, encoding="utf-8"))
        self.assertEqual(report["selected"], [])
        self.assertEqual(report["message"], "")

    def test_single_source_failure_does_not_block_others(self):
        good = newsnow.SourceResult(name="zhihu", label="知乎", platform="zhihu", ok=True,
                                    status="ok", items=[
                                        item("zhihu", "zhihu",
                                             "美商务部发布限制远程获取先进半导体相关措施，中方回应",
                                             "https://a/1")])
        bad = newsnow.SourceResult(name="cls-hot", label="财联社热榜", platform="cls", ok=False,
                                   status="provider_error", error="boom")
        good2 = newsnow.SourceResult(name="tencent-hot", label="腾讯热榜", platform="tencent", ok=True,
                                     status="ok", items=[
                                         item("tencent-hot", "tencent",
                                              "美商务部发布限制远程获取先进半导体相关措施，中方回应",
                                              "https://c/1")])

        def fake_fetch_all():
            return [good, bad, good2]

        with mock.patch.object(monitor_run, "fetch_all", fake_fetch_all):
            code = monitor_run.run(state_path=self.state, report_path=self.report, now_ms=NOW_MS)
        self.assertEqual(code, 0)
        report = json.load(open(self.report, encoding="utf-8"))
        self.assertFalse(report["telegram_sent"])
        self.assertEqual(len(report["selected"]), 0)   # 只剩 2 个平台，不到 3 个
        failed = [s for s in report["sources"] if not s["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "provider_error")

    def test_send_without_secret_is_config_error(self):
        path = self._write_fixture()
        env = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            code = monitor_run.run(fixtures=path, state_path=self.state, report_path=self.report,
                                   send=True, now_ms=NOW_MS)
        self.assertEqual(code, 5)


if __name__ == "__main__":
    unittest.main()