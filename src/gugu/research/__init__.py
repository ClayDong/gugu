"""研究层能力：只产出可审计的研究结果，不直接驱动交易执行。"""

from .ledger import append_record, build_record, new_run_id

__all__ = ["append_record", "build_record", "new_run_id"]
