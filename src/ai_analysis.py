"""Run a batch of optional AI analyses against a stable catalog snapshot."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from src.preview import PreviewError
from src.work_queue import bounded_results


def analyze_batch(infos, *, make_analyzer, has_result, save_result, progress, cancelled, workers):
    """Reuse matching results and retain completed work when a batch is cancelled."""
    analyzer = None
    analyzed = reused = failed = 0
    first_error = None

    def pending():
        nonlocal analyzer, reused, failed, first_error
        for info in infos:
            if cancelled():
                return
            try:
                before = info.path.stat()
                modified_at = datetime.fromtimestamp(before.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                if (before.st_size, modified_at) != (info.size_bytes, info.modified_at):
                    raise PreviewError("原片已变更，请先重新扫描")
                if info.file_mtime_ns is not None and before.st_mtime_ns != info.file_mtime_ns:
                    raise PreviewError("原片已变更，请先重新扫描")
            except (OSError, PreviewError) as exc:
                failed += 1
                first_error = first_error or str(exc)
                progress(analyzed + reused + failed, len(infos), info.file_name)
                continue
            if has_result(info, before.st_mtime_ns):
                reused += 1
                progress(analyzed + reused + failed, len(infos), info.file_name)
                continue
            if analyzer is None:
                progress(analyzed + reused + failed, len(infos), info.file_name)
                analyzer = make_analyzer()
            if not cancelled():
                yield info, before

    def analyze(item):
        info, before = item
        result = analyzer.analyze(info)
        after = info.path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise PreviewError("分析时原片发生变化，请重新扫描")
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (info, before), future in bounded_results(pool, pending(), analyze, limit=workers, cancelled=cancelled):
            try:
                result = future.result()
            except Exception as exc:
                failed += 1
                first_error = first_error or str(exc)
            else:
                if save_result(info, before.st_mtime_ns, result):
                    analyzed += 1
                else:
                    failed += 1
                    first_error = first_error or "照片库记录已变化，请刷新后重试"
            progress(analyzed + reused + failed, len(infos), info.file_name)
    return analyzed, reused, failed, first_error
