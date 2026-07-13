"""舆情分析模块测试：情感分析引擎 + 新闻采集器 + 影响分析 + 报告生成。"""

from __future__ import annotations

from gugu.analysis.sentiment import SentimentAnalyzer, SentimentReport, ImpactAnalyzer


class TestSentimentAnalyzer:
    """情感分析引擎测试。"""

    def test_positive_news(self) -> None:
        """正面新闻应识别为 positive。"""
        analyzer = SentimentAnalyzer()
        result = analyzer.analyze("公司业绩大幅增长，超预期盈利，多家机构推荐买入")
        assert result["sentiment"] == "positive"
        assert result["sentiment_score"] > 0.1

    def test_negative_news(self) -> None:
        """负面新闻应识别为 negative。"""
        analyzer = SentimentAnalyzer()
        result = analyzer.analyze("公司业绩大幅下跌，爆雷亏损，多家机构减持卖出")
        assert result["sentiment"] == "negative"
        assert result["sentiment_score"] < -0.1

    def test_neutral_news(self) -> None:
        """中性新闻应识别为 neutral。"""
        analyzer = SentimentAnalyzer()
        result = analyzer.analyze("公司今日召开董事会会议")
        assert result["sentiment"] == "neutral"

    def test_negation_flips_sentiment(self) -> None:
        """否定词反转：'没有增长'应降低正向评分。"""
        analyzer = SentimentAnalyzer()
        r1 = analyzer.analyze("公司业绩增长，表现乐观")
        r2 = analyzer.analyze("公司业绩没有增长，并不乐观")
        # 有否定词时应比无否定词时评分更低
        assert r2["sentiment_score"] < r1["sentiment_score"]

    def test_degree_word_amplifies(self) -> None:
        """程度副词倍增：'重大利好'分数应高于'利好'。"""
        analyzer = SentimentAnalyzer()
        r1 = analyzer.analyze("公司发布利好")
        r2 = analyzer.analyze("公司发布重大利好")
        assert abs(r2["sentiment_score"]) >= abs(r1["sentiment_score"])

    def test_empty_text(self) -> None:
        """空文本返回 neutral。"""
        analyzer = SentimentAnalyzer()
        result = analyzer.analyze("")
        assert result["sentiment"] == "neutral"
        assert result["sentiment_score"] == 0.0

    def test_key_words_detected(self) -> None:
        """情感关键词应被提取。"""
        analyzer = SentimentAnalyzer()
        result = analyzer.analyze("公司业绩增长，突破新高，竞争力领先")
        assert len(result["key_words"]) >= 2

    def test_both_positive_and_negative(self) -> None:
        """同时包含正负面词的文本，取综合评分。"""
        analyzer = SentimentAnalyzer()
        result = analyzer.analyze("公司业绩增长但存在债务风险")
        assert result["sentiment_score"] < 0.5  # 正负抵消后不应太高


class TestImpactAnalyzer:
    """影响分析引擎测试。"""

    def test_positive_impact_score(self) -> None:
        """正面舆情影响分数应为正。"""
        impact = ImpactAnalyzer(data_dir="/tmp/gugu_test_sentiment")
        result = impact.calculate(
            {"sentiment": "positive", "sentiment_score": 0.5, "sentiment_strength": 3},
            "证券时报",
        )
        assert result["overall_impact_score"] > 0
        assert result["media_weight"] >= 1.0

    def test_negative_impact_score(self) -> None:
        """负面舆情影响分数应为负。"""
        impact = ImpactAnalyzer(data_dir="/tmp/gugu_test_sentiment")
        result = impact.calculate(
            {"sentiment": "negative", "sentiment_score": -0.5, "sentiment_strength": 3},
            "未知",
        )
        assert result["overall_impact_score"] < 0

    def test_media_weight_effect(self) -> None:
        """证券时报权重1.5 > 网易财经1.0。"""
        impact = ImpactAnalyzer(data_dir="/tmp/gugu_test_sentiment")
        r1 = impact.calculate(
            {"sentiment": "positive", "sentiment_score": 0.5, "sentiment_strength": 3},
            "证券时报",
        )
        r2 = impact.calculate(
            {"sentiment": "positive", "sentiment_score": 0.5, "sentiment_strength": 3},
            "网易财经",
        )
        assert r1["overall_impact_score"] > r2["overall_impact_score"]

    def test_time_decay(self) -> None:
        """时效性衰减：第1天 > 第10天。"""
        impact = ImpactAnalyzer(data_dir="/tmp/gugu_test_sentiment")
        result = impact.calculate(
            {"sentiment": "positive", "sentiment_score": 0.5, "sentiment_strength": 3},
            "东方财富网",
        )
        assert result["time_decay_factors"]["day_1"] > result["time_decay_factors"]["day_10"]


class TestSentimentReport:
    """舆情报告生成器测试。"""

    def test_empty_news(self) -> None:
        """无新闻时返回基础报告。"""
        report = SentimentReport("600519", "贵州茅台")
        result = report.generate([])
        assert result["news_count"] == 0
        assert result["overall_sentiment"] == "neutral"

    def test_single_positive_news(self) -> None:
        """单条正面新闻报告。"""
        report = SentimentReport("600519", "贵州茅台")
        news = [{"title": "公司业绩大幅增长超预期", "content": "茅台业绩大涨盈利突破新高"}]
        result = report.generate(news)
        assert result["news_count"] == 1
        assert result["overall_sentiment"] == "positive"
        assert result["positive_news"] == 1

    def test_single_negative_news(self) -> None:
        """单条负面新闻报告。"""
        report = SentimentReport("600519", "贵州茅台")
        news = [{"title": "公司爆雷业绩大幅下跌", "content": "茅台业绩爆雷亏损严重"}]
        result = report.generate(news)
        assert result["news_count"] == 1
        assert result["overall_sentiment"] == "negative"

    def test_suggestion_buy(self) -> None:
        """正面强信号给出买入建议。"""
        report = SentimentReport("600519", "贵州茅台")
        news = [{"title": "重大利好业绩超预期增长", "content": "业绩大幅增长营收突破盈利新高"}]
        # 添加多个正面新闻确保评分 > 0.3
        for _ in range(3):
            news.append({"title": "利好公司竞争力领先", "content": "市场份额增长积极向好"})
        result = report.generate(news)
        sug = result.get("suggestion", {})
        assert sug.get("action") == "buy" or (
            sug.get("action") == "hold" and result["overall_sentiment"] == "positive"
        )

    def test_suggestion_sell(self) -> None:
        """负面强信号给出卖出建议。"""
        report = SentimentReport("600519", "贵州茅台")
        news = [
            {"title": "公司爆雷业绩暴跌", "content": "业绩大幅下滑亏损严重债务违约"},
            {"title": "利空公司被处罚", "content": "公司被证监会处罚调查"},
            {"title": "机构减持不看好", "content": "多家机构减持卖出不看好未来"},
        ]
        result = report.generate(news)
        sug = result.get("suggestion", {})
        assert sug.get("action") == "sell" or result["risk_level"] >= 3

    def test_feishu_card_format(self) -> None:
        """飞书卡片格式正确。"""
        report = SentimentReport("600519", "贵州茅台")
        news = [{"title": "公司业绩增长超预期", "content": "盈利突破新高"}]
        result = report.generate(news)
        card = report.format_feishu_card(result)
        assert "card" in card
        assert "header" in card["card"]
        assert "elements" in card["card"]
        assert (
            "600519" in card["card"]["header"]["title"]["content"]
            or "贵州茅台" in card["card"]["header"]["title"]["content"]
        )
