"""Seed training templates into the database.

Usage: python -m hub.chat_seed
"""
import os
import sys
import json
from datetime import datetime

# Ensure hub package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.database import get_db


TEMPLATES = [
    # PM 组
    {
        "id": "pm-proposal",
        "name": "项目提案：智能工厂改造",
        "description": "你是一个资深项目经理。请为智能工厂改造项目写一份完整的项目提案。\n\n项目背景：预算 500 万，周期 6 个月。\n\n输出要求：包含项目概述、目标、范围、时间表、风险清单、预算分配。",
        "role": "pm",
        "expected_output": "项目提案 Word 文档",
        "skill_template": "你是资深项目经理。工作流：接到需求 → 查历史 → 生成方案 → 输出格式。输出使用 Markdown 格式，分章节。",
        "time_limit_minutes": 75,
        "sort_order": 1,
        "task_type": "general",
        "created_at": datetime.now().isoformat(),
    },
    {
        "id": "pm-skill-design",
        "name": "PM Skill 设计草稿",
        "description": "设计一个 PM 角色的 Skill 文档。包含：角色定义、工作流、输出格式、约束条件。",
        "role": "pm",
        "expected_output": "Skill 设计文档",
        "skill_template": "你是 Skill 设计专家。按照 Role → Context → Workflow → Output 的结构编写 Skill。",
        "time_limit_minutes": 15,
        "sort_order": 2,
        "task_type": "general",
        "created_at": datetime.now().isoformat(),
    },
    # 开发组
    {
        "id": "dev-code",
        "name": "文件批量处理工具",
        "description": "写一个 Python 文件批量处理工具。功能：遍历目录、筛选文件、批量操作（重命名/移动/删除）、生成操作报告。要求：使用 argparse 接受命令行参数，包含单元测试。",
        "role": "developer",
        "expected_output": "Python 脚本 + 单元测试",
        "skill_template": "你是高级开发工程师。工作流：需求描述 → 生成代码 → Code Review → 测试。代码需符合 PEP 8，包含类型注解和文档字符串。",
        "time_limit_minutes": 60,
        "sort_order": 3,
        "task_type": "general",
        "created_at": datetime.now().isoformat(),
    },
    {
        "id": "dev-review",
        "name": "Code Review 报告",
        "description": "对提供的代码进行 Code Review。检查：代码风格、潜在 bug、性能问题、可维护性。输出结构化的 review 报告。",
        "role": "developer",
        "expected_output": "Code Review 文档",
        "skill_template": "你是资深代码审查员。按以下结构输出：1. 总体评价 2. 代码风格 3. 潜在 bug 4. 性能问题 5. 改进建议。",
        "time_limit_minutes": 15,
        "sort_order": 4,
        "task_type": "general",
        "created_at": datetime.now().isoformat(),
    },
    # 硬件组
    {
        "id": "hw-fault-analysis",
        "name": "GS500 故障日志分析",
        "description": "分析 GS500 设备的故障日志。场景：Camera module 初始化失败，I2C 总线超时。输出：故障分析报告 + 调试报告。",
        "role": "hardware",
        "expected_output": "故障分析报告 + 调试报告",
        "skill_template": "你是硬件调试工程师。工作流：故障现象 → 分析日志 → 排查步骤 → 调试报告。使用 8D 方法论。",
        "time_limit_minutes": 50,
        "sort_order": 5,
        "task_type": "general",
        "created_at": datetime.now().isoformat(),
    },
    # 销售/管理组
    {
        "id": "sales-proposal",
        "name": "客户方案 + PPT",
        "description": "根据客户需求生成方案文档。背景：客户咨询智能工厂改造方案。输出：方案文档（Markdown），包含技术方案、实施计划、报价估算。",
        "role": "sales",
        "expected_output": "客户方案文档",
        "skill_template": "你是市场经理。工作流：分析客户需求 → 匹配产品 → 生成方案。输出专业、简洁，面向非技术决策者。",
        "time_limit_minutes": 30,
        "sort_order": 6,
        "task_type": "general",
        "created_at": datetime.now().isoformat(),
    },
    {
        "id": "quality-8d",
        "name": "8D 品质报告",
        "description": "写一份 8D 品质报告。问题：某批次产品外观不良。按 8D 步骤：D1 团队、D2 问题定义(5W2H)、D3 临时措施、D4 根因分析(5Why)、D5 永久对策、D6 验证、D7 预防、D8 表彰。",
        "role": "sales",
        "expected_output": "8D 报告文档",
        "skill_template": "你是品质工程师。按 8D 方法论结构化输出。每个 D 步骤都要具体、可执行。",
        "time_limit_minutes": 30,
        "sort_order": 7,
        "task_type": "general",
        "created_at": datetime.now().isoformat(),
    },
]


def seed():
    """Insert or update all templates."""
    with get_db() as conn:
        cursor = conn.cursor()
        for tpl in TEMPLATES:
            cursor.execute("""
                INSERT OR REPLACE INTO task_templates
                (id, name, description, role, expected_output, skill_template,
                 time_limit_minutes, sort_order, task_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tpl["id"], tpl["name"], tpl["description"], tpl["role"],
                tpl["expected_output"], tpl["skill_template"],
                tpl["time_limit_minutes"], tpl["sort_order"],
                tpl["task_type"], tpl["created_at"],
            ))
        conn.commit()
    print(f"[seed] Inserted {len(TEMPLATES)} training templates")


if __name__ == "__main__":
    seed()
