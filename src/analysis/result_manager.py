import os
from datetime import datetime

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'results')
RESULTS_DIR = os.path.abspath(RESULTS_DIR)


def save_analysis_result(content: str) -> str:
    """保存分析结果到 results 目录，返回文件路径"""
    if not os.path.exists(RESULTS_DIR):
        os.makedirs(RESULTS_DIR)
    filename = datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
    filepath = os.path.join(RESULTS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


def get_latest_results(n=3):
    """获取最近n次分析结果内容，按时间降序排列"""
    if not os.path.exists(RESULTS_DIR):
        return []
    files = [f for f in os.listdir(RESULTS_DIR) if f.endswith(".md")]
    files.sort(reverse=True)
    latest_files = files[:n]
    results = []
    for fname in latest_files:
        with open(os.path.join(RESULTS_DIR, fname), "r", encoding="utf-8") as f:
            results.append(f.read())
    return results 