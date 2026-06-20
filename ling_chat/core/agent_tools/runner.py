import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from typing import Any

from ling_chat.core.agent_tools.registry import SimpleToolRegistry
from ling_chat.core.ai_service.game_system.game_status import GameStatus
from ling_chat.core.llm_providers.manager import LLMManager
from ling_chat.core.logger import logger
from ling_chat.core.messaging.broker import message_broker


class SimpleAgentRunner:
    SANDBOX_TOOL_NAMES = {
        "sandbox_read_file",
        "sandbox_write_file",
        "sandbox_list_files",
        "sandbox_delete_file",
        "sandbox_execute_command",
    }
    SANDBOX_MUTATION_TOOLS = {
        "sandbox_write_file",
        "sandbox_delete_file",
        "sandbox_execute_command",
    }
    AI_IDE_TOOL_NAMES = SANDBOX_TOOL_NAMES | {"update_plan"}
    CHAT_MODE_ALLOWED_TOOL_NAMES = {
        "get_current_status",
        "get_current_scene",
        "get_memory",
        "get_current_time",
        "get_schedules",
        "get_updated_plan",
        "schedule_add_todo",
        "get_memory_notes",
        "memory_add_note",
        "list_scenes",
        "list_characters",
        "switch_scene",
        "switch_character",
    }

    def __init__(self, llm_model: LLMManager, game_status: GameStatus):
        self.llm_model = llm_model
        self.registry = SimpleToolRegistry(game_status)
        self.max_result_chars = int(os.environ.get("SIMPLE_TOOLS_MAX_RESULT_CHARS", "12000"))
        self.planner_timeout_seconds = int(os.environ.get("SIMPLE_TOOLS_PLANNER_TIMEOUT", "45"))

    def is_enabled(self) -> bool:
        return os.environ.get("ENABLE_SIMPLE_TOOLS", "true").lower() == "true"

    async def enrich_context_if_needed(
        self,
        messages: list[dict[str, Any]],
        user_message: str,
        client_id: str | None = None,
        code_mode: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.is_enabled() or not user_message.strip():
            return messages

        max_rounds = int(os.environ.get("SIMPLE_TOOLS_MAX_ROUNDS", "3"))
        current_messages = list(messages)
        executed_tools: list[dict[str, Any]] = []
        sandbox_request = self._is_sandbox_request(user_message)
        plan_followup_request = self._is_plan_followup_request(user_message, messages)
        first_keyword_plan = self._keyword_plan(user_message)
        if not code_mode and sandbox_request:
            keyword_tool = str(
                (first_keyword_plan or {}).get("tool")
                or (first_keyword_plan or {}).get("name")
                or (first_keyword_plan or {}).get("action")
                or ""
            )
            if first_keyword_plan and self._is_tool_allowed_in_mode(keyword_tool, code_mode=False):
                sandbox_request = False
            else:
                return self._with_chat_mode_ide_guidance(current_messages)
        if not code_mode:
            plan_followup_request = False
        if code_mode:
            sandbox_request = sandbox_request or self._is_code_mode_sandbox_request(user_message, messages)
            plan_followup_request = plan_followup_request or self._is_plan_followup_request(user_message, messages)
        if sandbox_request or plan_followup_request:
            max_rounds = max(max_rounds, 6)
        if code_mode:
            max_rounds = max(max_rounds, 10)

        for round_num in range(max_rounds):
            plan = first_keyword_plan if round_num == 0 else None
            if plan is None and sandbox_request:
                plan = self._deterministic_sandbox_plan(
                    user_message, current_messages, executed_tools
                )
            if plan is None:
                plan = await self._plan_tool_call(
                    current_messages,
                    user_message,
                    sandbox_only=sandbox_request and not plan_followup_request,
                    force_tool=(
                        (sandbox_request and not self._sandbox_task_completed(user_message, executed_tools))
                        or (
                            plan_followup_request
                            and not self._plan_followup_completed(user_message, executed_tools)
                        )
                        or (code_mode and not self._code_mode_task_completed(executed_tools))
                    ),
                    code_mode=code_mode,
                )

            if (
                sandbox_request
                and self._is_no_tool_plan(plan)
                and plan.get("_raw")
                and self._is_sandbox_write_request(user_message)
            ):
                raw_code = self._extract_code_from_text(str(plan.get("_raw") or ""))
                target_path = self._extract_sandbox_path(user_message, current_messages, executed_tools)
                if raw_code and target_path:
                    plan = {
                        "tool": "sandbox_write_file",
                        "arguments": {"path": target_path, "content": raw_code},
                    }

            tool_name = plan.get("tool") or plan.get("name") or plan.get("action")
            if not tool_name or str(tool_name).lower() in {"none", "null", "no_tool"}:
                break

            arguments = plan.get("arguments") or plan.get("args") or {}
            if not isinstance(arguments, dict):
                arguments = {}

            tool_name = str(tool_name)
            if not self._is_tool_allowed_in_mode(tool_name, code_mode):
                current_messages = self._with_chat_mode_ide_guidance(current_messages, tool_name)
                break

            logger.info(f"SimpleTools: round {round_num + 1}/{max_rounds}, running tool {tool_name}")
            call_id = uuid.uuid4().hex
            await self._publish_tool_event(client_id, call_id, tool_name, arguments, "running")
            result = await self.registry.execute(tool_name, arguments)
            result = self._annotate_tool_result(tool_name, arguments, result, executed_tools)
            await self._publish_tool_event(
                client_id,
                call_id,
                tool_name,
                arguments,
                "success" if result.get("ok") else "error",
                result,
            )

            # 处理需要通知前端的修改类工具
            await self._handle_state_change(tool_name, result, client_id)

            executed_tools.append({"tool": tool_name, "result": result})

            result_text = json.dumps(result, ensure_ascii=False, indent=2)
            if len(result_text) > self.max_result_chars:
                result_text = result_text[: self.max_result_chars] + "\n...[truncated]"

            current_messages = current_messages + [
                {
                    "role": "user",
                    "content": (
                        f"[Internal tool result #{round_num + 1}]\n"
                        f"Tool: {tool_name}\n"
                        "The following JSON was retrieved by LingChat's safe internal tool system.\n"
                        "Answer the user's latest message using this JSON as the highest-priority factual source.\n"
                        "Rules:\n"
                        "1. Do not invent scene, background, schedule, character, or status details that are absent from the JSON.\n"
                        "2. If a relevant value is empty, null, or an empty string, say it is not set yet.\n"
                        "3. For get_current_status, treat result.scene.current_scene as the current scene. "
                        "Treat result.media.background as the current background, not as a scene name.\n"
                        "4. In chat mode, keep the normal LingChat dialogue format with emotion tags. "
                        "In code mode, be concise and factual, and mention tool outcomes when useful.\n"
                        f"{result_text}"
                    ),
                }
            ]

            if not code_mode and sandbox_request and self._sandbox_task_completed(user_message, executed_tools):
                break
            if not code_mode and plan_followup_request and self._plan_followup_completed(user_message, executed_tools):
                break
            if code_mode and self._code_mode_task_completed(executed_tools):
                break

        if sandbox_request and not executed_tools:
            current_messages = current_messages + [
                {
                    "role": "user",
                    "content": (
                        "[Internal tool guidance]\n"
                        "The latest user request is a coding or sandbox task, but no sandbox tool was executed. "
                        "Do not stream full source code as normal dialogue. Briefly say the sandbox tool call did not complete "
                        "and ask the user to retry or make the request more specific."
                    ),
                }
            ]
        elif (
            sandbox_request
            and self._is_sandbox_write_request(user_message)
            and not self._sandbox_task_completed(user_message, executed_tools)
        ):
            current_messages = current_messages + [
                {
                    "role": "user",
                    "content": (
                        "[Internal tool guidance]\n"
                        "The latest user request asked to create or modify sandbox code, but no write/execute tool "
                        "completed. Do not output full source code as chat text. Briefly say the sandbox edit did not "
                        "complete automatically, mention any file list/read result if useful, and ask for the exact file "
                        "and change if needed."
                    ),
                }
            ]

        return current_messages

    async def _handle_state_change(
        self, tool_name: str, result: dict[str, Any], client_id: str | None
    ) -> None:
        """处理修改类工具的状态变更通知"""
        if not client_id or not result.get("ok"):
            return

        payload = result.get("result") if isinstance(result.get("result"), dict) else result

        if tool_name == "switch_scene" and payload.get("scene"):
            await message_broker.publish(
                client_id,
                {
                    "type": "scene_change",
                    "scene": payload["scene"],
                },
            )
        elif tool_name == "switch_character" and payload.get("character"):
            await message_broker.publish(
                client_id,
                {
                    "type": "character_change",
                    "character": payload["character"],
                },
            )
        elif tool_name in {"schedule_add_todo", "memory_add_note", "update_plan"}:
            await message_broker.publish(
                client_id,
                {
                    "type": "schedule_updated",
                    "source": "tool",
                    "tool": tool_name,
                    "result": payload,
                },
            )

    def _is_tool_allowed_in_mode(self, tool_name: str, code_mode: bool) -> bool:
        if code_mode:
            return True
        return tool_name in self.CHAT_MODE_ALLOWED_TOOL_NAMES

    def _with_chat_mode_ide_guidance(
        self, messages: list[dict[str, Any]], tool_name: str | None = None
    ) -> list[dict[str, Any]]:
        blocked = f" Blocked tool: {tool_name}." if tool_name else ""
        return messages + [
            {
                "role": "user",
                "content": (
                    "[Internal tool policy]\n"
                    "Chat mode has AI IDE tools disabled. Do not use sandbox, file editing, command execution, "
                    f"or update_plan tools in chat mode.{blocked} If the user wants coding, sandbox, file, "
                    "command, or IDE-style plan work, briefly ask them to switch on Code mode. Non-programming "
                    "LingChat tools such as memory, schedules, current status, scenes, and character switching "
                    "remain available."
                ),
            }
        ]

    async def _publish_tool_event(
        self,
        client_id: str | None,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        if not client_id:
            return
        safe_arguments = self._safe_tool_arguments(tool_name, arguments)
        preview = ""
        if result is not None:
            preview = json.dumps(result, ensure_ascii=False)
            if len(preview) > 260:
                preview = preview[:260] + "..."
        await message_broker.publish(
            client_id,
            {
                "type": "tool_call",
                "id": call_id,
                "tool": tool_name,
                "arguments": safe_arguments,
                "status": status,
                "ok": result.get("ok") if result else None,
                "summary": self._tool_event_summary(tool_name, arguments, status, result),
                "preview": preview,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            },
        )

    def _tool_event_summary(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        result: dict[str, Any] | None = None,
    ) -> str:
        target = (
            arguments.get("path")
            or arguments.get("command")
            or arguments.get("text")
            or arguments.get("title")
            or ""
        )
        target_text = str(target) if target else tool_name
        if status == "running":
            running_labels = {
                "sandbox_write_file": "正在写入",
                "sandbox_read_file": "正在读取",
                "sandbox_list_files": "正在列出",
                "sandbox_delete_file": "正在删除",
                "sandbox_execute_command": "正在运行",
                "update_plan": "正在更新计划",
                "get_updated_plan": "正在读取计划",
            }
            return f"{running_labels.get(tool_name, '正在执行')} {target_text}".strip()

        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        if not ok:
            return f"{tool_name} 执行失败"

        payload = result.get("result") if isinstance(result.get("result"), dict) else result
        if tool_name == "sandbox_write_file" and isinstance(payload, dict):
            path = payload.get("path") or target_text
            added = int(payload.get("lines_added") or 0)
            removed = int(payload.get("lines_removed") or 0)
            verb = "已创建" if payload.get("created") else "已编辑"
            return f"{verb} {path} (+{added} -{removed})"
        if tool_name == "sandbox_execute_command" and isinstance(payload, dict):
            return f"命令完成 exit {payload.get('returncode', 0)}"
        if tool_name == "sandbox_list_files" and isinstance(payload, dict):
            return f"已列出 {payload.get('path', '.')}：{len(payload.get('items') or [])} 项"
        if tool_name == "sandbox_read_file" and isinstance(payload, dict):
            return f"已读取 {payload.get('path') or target_text}"
        if tool_name == "sandbox_delete_file" and isinstance(payload, dict):
            deleted = payload.get("deleted_count") or payload.get("deleted_files") or 1
            return f"已删除 {deleted} 项"
        return f"{tool_name} 执行完成"

    def _safe_tool_arguments(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        safe_arguments = dict(arguments)
        if tool_name == "sandbox_write_file" and isinstance(safe_arguments.get("content"), str):
            content = safe_arguments["content"]
            safe_arguments["content_preview"] = content[:160]
            safe_arguments["content_size"] = len(content)
            safe_arguments.pop("content", None)
        return safe_arguments

    async def _plan_tool_call(
        self,
        messages: list[dict[str, Any]],
        user_message: str,
        sandbox_only: bool = False,
        force_tool: bool = False,
        code_mode: bool = False,
    ) -> dict[str, Any]:
        tool_results = self._extract_recent_tool_results(messages)
        planner_messages = [
            {
                "role": "system",
                "content": self._build_planner_prompt(
                    sandbox_only=sandbox_only,
                    force_tool=force_tool,
                    code_mode=code_mode,
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Latest user message:\n{user_message}\n\n"
                    f"Recent dialogue context:\n{self._extract_recent_dialogue(messages)}\n\n"
                    f"Recent internal tool results, if any:\n{tool_results or 'None'}"
                ),
            },
        ]

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.llm_model.process_message, planner_messages),
                timeout=self.planner_timeout_seconds,
            )
            parsed = self._parse_json(raw or "")
            if parsed is None:
                logger.debug(f"SimpleTools: planner returned non-json response: {raw}")
                return {"tool": "none", "arguments": {}, "_raw": raw or ""}
            return parsed
        except asyncio.TimeoutError:
            logger.warning(
                f"SimpleTools: planner timed out after {self.planner_timeout_seconds}s"
            )
            return {"tool": "none", "arguments": {}, "_timeout": True}
        except Exception as exc:
            logger.warning(f"SimpleTools: planner failed, continuing without tools: {exc}")
            return {"tool": "none", "arguments": {}}

    def _build_planner_prompt(
        self,
        sandbox_only: bool = False,
        force_tool: bool = False,
        code_mode: bool = False,
    ) -> str:
        if sandbox_only:
            tools = self.registry.describe_for_prompt(self.SANDBOX_TOOL_NAMES)
        elif code_mode:
            tools = self.registry.describe_for_prompt()
        else:
            tools = self.registry.describe_for_prompt(self.CHAT_MODE_ALLOWED_TOOL_NAMES)
        rules = [
            "You are LingChat's internal tool planner. Decide whether the assistant needs one safe internal tool "
            "to answer the user's latest message.",
            "Return ONLY compact JSON. Do not use markdown.",
            "If no tool is needed, return: {\"tool\":\"none\",\"arguments\":{}}",
            "If a tool is useful, return: {\"tool\":\"tool_name\",\"arguments\":{...}}",
            "Use at most one tool per planning round. Multiple rounds may run in one response, so keep using tools while the original request still needs action.",
            "For requests that ask to create, write, edit, save, delete, remove, clear, run, test, or inspect code/files, use a sandbox_* tool.",
            "Do not answer coding requests by placing full source code in chat text.",
            "When editing an existing file, preserve the file and apply a real change requested by the dialogue context.",
            "If the latest message says a vague edit like 'modify it', infer the concrete requested changes from recent dialogue context.",
            "Never call sandbox_write_file with content identical to the most recent sandbox_read_file result for the same path.",
            "For todo requests that ask to add, create, remember, or record a task, use schedule_add_todo.",
            "For checklist-style planning requests that ask to update, revise, create, or show progress on a plan, use update_plan.",
            "For requests that ask to read the current Updated Plan, use get_updated_plan.",
            "If the user says continue/继续 and an Updated Plan exists, continue the next incomplete plan item instead of asking the user to say continue again.",
            "For coding plans, update_plan is not completion by itself. After creating or reading the plan, continue with sandbox_* tools to actually write, edit, run, or test the files.",
            "If sandbox_read_file reports File not found for a planned file, use sandbox_write_file to create that file instead of only explaining the problem.",
            "Do not use schedule_add_todo for an Updated Plan; todo items and the Updated Plan are separate stores.",
            "Memory tools have two separate stores. get_memory reads the character's automatic role memory bank.",
            "get_memory_notes and memory_add_note read/write the manual memory library in schedules.json memoryNotes.",
            "For requests that ask to remember, save a preference, record a durable note, or add a memory note, use memory_add_note.",
            "For requests that ask to read saved memory notes, the memory library, or Codex-like memories, use get_memory_notes.",
            "For requests that ask about the character's own automatic memories, long-term memory, short-term memory, user info, or promises, use get_memory.",
        ]
        if not code_mode and not sandbox_only:
            rules.extend(
                [
                    "CHAT MODE TOOL POLICY: AI IDE tools are disabled.",
                    "Never choose sandbox_* tools or update_plan in chat mode.",
                    "If the user asks to edit files, run commands, inspect sandbox files, write code, test code, or update an IDE-style plan, return {\"tool\":\"none\",\"arguments\":{}}.",
                    "Character switching, scene switching, memory, schedules, current status, and time tools are allowed.",
                ]
            )
        if sandbox_only:
            rules.extend(
                [
                    "This is a coding or sandbox request. Choose only one sandbox_* tool.",
                    "For creating code, choose sandbox_write_file with a short relative path and complete file content.",
                    "For running existing code or commands, choose sandbox_execute_command.",
                    "For deleting files or clearing the sandbox, choose sandbox_delete_file. Use recursive=true for directories.",
                    "For checking existing sandbox files, choose sandbox_list_files or sandbox_read_file.",
                ]
            )
        if code_mode:
            rules.extend(
                [
                    "CODE MODE: prefer decisive coding actions over explanation.",
                    "For small standalone coding requests, prefer sandbox_write_file directly instead of update_plan.",
                    "Use update_plan only for multi-step tasks where the user explicitly wants a visible checklist.",
                    "If a previous tool result says File not found for a planned path, choose sandbox_write_file and create the missing file.",
                    "After writing code, prefer sandbox_execute_command for a quick syntax or run check when practical.",
                    "Do not stop after get_updated_plan or sandbox_read_file unless the user's request was only to inspect.",
                ]
            )
        if force_tool:
            rules.append("A tool call is required unless the request is impossible or unsafe.")
        return "\n".join(rules) + "\nAvailable tools:\n" + tools

    def _keyword_plan(self, user_message: str) -> dict[str, Any] | None:
        text = user_message.lower()
        status_keywords = [
            "当前状态",
            "现在状态",
            "当前背景",
            "现在背景",
            "当前场景",
            "现在场景",
            "什么场景",
            "消息数量",
            "当前角色",
            "现在角色",
        ]
        schedule_add_keywords = ["添加待办", "新增待办", "创建待办", "记个待办", "记一个待办", "加入待办", "待办事项"]
        schedule_keywords = ["日程", "待办", "todo", "重要日", "安排"]
        updated_plan_keywords = ["updated plan", "当前计划", "现在计划", "计划列表", "进度计划"]
        scene_keywords = ["有哪些场景", "场景列表", "可用场景"]
        character_keywords = ["有哪些角色", "角色列表", "可用角色"]
        memory_add_keywords = ["记住", "帮我记住", "添加记忆", "新增记忆", "保存记忆", "记录记忆", "记一下"]
        memory_note_keywords = ["记忆笔记", "记忆库", "手动记忆", "保存的记忆", "已保存记忆", "日程记忆", "codex记忆", "codex 记忆"]
        role_memory_keywords = ["角色记忆", "自动记忆", "长期记忆", "短期记忆", "用户信息", "承诺", "记忆银行"]
        current_scene_keywords = ["当前场景描述", "现在在哪", "在哪里", "在什么地方"]
        time_keywords = ["现在几点", "当前时间", "今天几号", "现在时间", "日期"]
        switch_scene_keywords = ["切换场景", "换场景", "去", "切换到场景"]
        switch_character_keywords = ["切换角色", "换角色", "变成", "切换到角色"]
        sandbox_list_keywords = ["列出文件", "文件列表", "目录", "有什么文件", "沙盒文件"]

        if any(keyword in text for keyword in status_keywords):
            return {"tool": "get_current_status", "arguments": {}}
        if any(keyword in text for keyword in current_scene_keywords):
            return {"tool": "get_current_scene", "arguments": {}}
        if any(keyword in text for keyword in memory_add_keywords):
            content = self._extract_memory_note_content(user_message)
            if content:
                return {"tool": "memory_add_note", "arguments": {"content": content}}
        if any(keyword in text for keyword in memory_note_keywords):
            return {"tool": "get_memory_notes", "arguments": {}}
        if any(keyword in text for keyword in role_memory_keywords):
            return {"tool": "get_memory", "arguments": {}}
        if any(keyword in text for keyword in time_keywords):
            return {"tool": "get_current_time", "arguments": {}}
        if any(keyword in text for keyword in updated_plan_keywords):
            return {"tool": "get_updated_plan", "arguments": {}}
        if any(keyword in text for keyword in schedule_add_keywords):
            todo_text = self._extract_todo_text(user_message)
            if todo_text:
                return {"tool": "schedule_add_todo", "arguments": {"text": todo_text}}
        if any(keyword in text for keyword in schedule_keywords):
            return {"tool": "get_schedules", "arguments": {}}
        if any(keyword in text for keyword in scene_keywords):
            return {"tool": "list_scenes", "arguments": {"limit": 10}}
        if any(keyword in text for keyword in character_keywords):
            return {"tool": "list_characters", "arguments": {"limit": 10}}
        if any(keyword in text for keyword in switch_scene_keywords):
            # 尝试提取场景名（简单启发式：关键词后面的内容）
            for kw in switch_scene_keywords:
                if kw in text:
                    idx = text.find(kw) + len(kw)
                    scene_name = user_message[idx:].strip()
                    if scene_name:
                        return {"tool": "switch_scene", "arguments": {"scene_name": scene_name}}
        if any(keyword in text for keyword in switch_character_keywords):
            for kw in switch_character_keywords:
                if kw in text:
                    idx = text.find(kw) + len(kw)
                    char_name = user_message[idx:].strip()
                    if char_name:
                        return {"tool": "switch_character", "arguments": {"character_name": char_name}}

        # 沙盒工具关键词匹配（只匹配无参查询类，写文件/执行命令等需要具体参数，交给 LLM planner）
        if any(keyword in text for keyword in sandbox_list_keywords):
            return {"tool": "sandbox_list_files", "arguments": {"path": "."}}

        return None

    def _is_sandbox_request(self, user_message: str) -> bool:
        text = user_message.lower()
        if self._is_sandbox_delete_request(user_message):
            return True
        direct_code_terms = [
            "代码",
            "编程",
            "脚本",
            "沙盒",
            "python",
            "javascript",
            "typescript",
            "pygame",
            "html",
            "css",
            "node",
            "npm",
            "pnpm",
            "game",
        ]
        if any(keyword in text for keyword in direct_code_terms):
            return True

        action_terms = ["写一个", "写个", "创建", "新建", "保存", "运行", "执行", "测试", "修改", "编辑"]
        target_terms = ["程序", "文件", "游戏", "项目", "命令", "目录", "网页"]
        return any(action in text for action in action_terms) and any(target in text for target in target_terms)

    def _is_code_mode_sandbox_request(self, user_message: str, messages: list[dict[str, Any]]) -> bool:
        text = user_message.lower().strip()
        if self._is_sandbox_request(user_message):
            return True
        if text in {"继续", "继续吧", "继续执行", "继续完成", "接着", "接着做"}:
            recent = f"{self._extract_recent_dialogue(messages)}\n{self._extract_recent_tool_results(messages)}".lower()
            return any(term in recent for term in ["sandbox_", ".py", ".js", ".html", "代码", "文件", "游戏"])
        code_terms = ["代码", "文件", "脚本", "程序", "游戏", "运行", "测试", "修复", "bug", "python", "pygame", ".py", ".js", ".html"]
        return any(term in text for term in code_terms)

    def _is_no_tool_plan(self, plan: dict[str, Any]) -> bool:
        tool_name = str(plan.get("tool") or plan.get("name") or plan.get("action") or "").lower()
        return tool_name in {"", "none", "null", "no_tool"}

    def _is_sandbox_write_request(self, user_message: str) -> bool:
        text = user_message.lower()
        action_terms = ["写", "创建", "新建", "保存", "修改", "编辑", "优化", "改", "补充", "加上", "加入"]
        target_terms = ["代码", "文件", "脚本", "程序", "游戏", ".py", ".js", ".ts", ".html", ".css"]
        return any(term in text for term in action_terms) and any(term in text for term in target_terms)

    def _is_sandbox_delete_request(self, user_message: str) -> bool:
        text = user_message.lower()
        delete_terms = [
            "delete",
            "remove",
            "clear",
            "clean",
            "删除",
            "删掉",
            "清空",
            "清理",
            "全部删除",
            "全都删除",
        ]
        return any(term in text for term in delete_terms)

    def _is_sandbox_clear_request(self, user_message: str) -> bool:
        text = user_message.lower()
        clear_terms = ["clear", "clean", "all", "清空", "清理", "全部", "全都", "所有"]
        return any(term in text for term in clear_terms)

    def _is_plan_followup_request(self, user_message: str, messages: list[dict[str, Any]]) -> bool:
        text = user_message.lower().strip()
        if any(term in text for term in ["plan", "计划", "下一步"]):
            return True
        if text not in {"继续", "继续吧", "继续执行", "继续完成", "接着", "接着做"}:
            return False

        recent_dialogue = self._extract_recent_dialogue(messages).lower()
        recent_tools = self._extract_recent_tool_results(messages).lower()
        plan_terms = ["plan", "计划", "update_plan", "get_updated_plan", "sandbox_", ".py", ".js", ".html", "代码"]
        return any(term in recent_dialogue or term in recent_tools for term in plan_terms)

    def _plan_followup_completed(self, user_message: str, executed_tools: list[dict[str, Any]]) -> bool:
        if not executed_tools:
            return False
        successful_tools = [
            str(item.get("tool") or "")
            for item in executed_tools
            if isinstance(item.get("result"), dict) and item["result"].get("ok", False)
        ]
        if any(tool in self.SANDBOX_MUTATION_TOOLS for tool in successful_tools):
            return True
        text = user_message.lower()
        if text.strip() in {"继续", "继续吧", "继续执行", "继续完成", "接着", "接着做"}:
            return False
        coding_plan_terms = [
            "写",
            "创建",
            "新建",
            "代码",
            "文件",
            "程序",
            "游戏",
            "棋",
            "python",
            "pygame",
            ".py",
            ".js",
            ".html",
        ]
        if any(term in text for term in coding_plan_terms):
            return False
        return "update_plan" in successful_tools and not any(
            tool in {"get_updated_plan", "sandbox_read_file", "sandbox_list_files"} for tool in successful_tools
        )

    def _code_mode_task_completed(self, executed_tools: list[dict[str, Any]]) -> bool:
        successful_tools = [
            str(item.get("tool") or "")
            for item in executed_tools
            if isinstance(item.get("result"), dict) and item["result"].get("ok", False)
        ]
        if "sandbox_execute_command" in successful_tools:
            return True
        if "sandbox_write_file" in successful_tools and len(executed_tools) >= 4:
            return True
        return False

    def _sandbox_task_completed(self, user_message: str, executed_tools: list[dict[str, Any]]) -> bool:
        if not executed_tools:
            return False
        last_tool = str(executed_tools[-1].get("tool") or "")
        last_result = executed_tools[-1].get("result") or {}
        if last_tool in self.SANDBOX_TOOL_NAMES and not last_result.get("ok", False):
            return False
        if self._is_sandbox_delete_request(user_message):
            return last_tool == "sandbox_delete_file"
        if last_tool == "sandbox_write_file":
            nested = last_result.get("result") if isinstance(last_result, dict) else None
            if isinstance(nested, dict) and nested.get("changed") is False:
                return False
        if self._is_sandbox_write_request(user_message):
            return last_tool in self.SANDBOX_MUTATION_TOOLS
        if any(term in user_message.lower() for term in ["运行", "执行", "测试", "run", "test"]):
            return last_tool == "sandbox_execute_command"
        return last_tool in self.SANDBOX_TOOL_NAMES

    def _deterministic_sandbox_plan(
        self,
        user_message: str,
        messages: list[dict[str, Any]],
        executed_tools: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        text = user_message.lower()
        target_path = self._extract_sandbox_path(user_message, messages, executed_tools)
        if not executed_tools and self._is_sandbox_delete_request(user_message):
            return {
                "tool": "sandbox_delete_file",
                "arguments": {
                    "path": "." if self._is_sandbox_clear_request(user_message) else (target_path or "."),
                    "recursive": True,
                },
            }

        if not executed_tools:
            if any(term in text for term in ["运行", "执行", "测试", "run", "test"]):
                if target_path:
                    return {
                        "tool": "sandbox_execute_command",
                        "arguments": {"command": self._command_for_path(target_path)},
                    }
                return {"tool": "sandbox_list_files", "arguments": {"path": "."}}

            if self._is_sandbox_write_request(user_message):
                if target_path and any(term in text for term in ["修改", "编辑", "优化", "改", "补充", "加上", "加入"]):
                    return {"tool": "sandbox_read_file", "arguments": {"path": target_path}}
                if target_path and self._recent_dialogue_has_edit_intent(messages):
                    return {"tool": "sandbox_read_file", "arguments": {"path": target_path}}
                if not target_path:
                    return {"tool": "sandbox_list_files", "arguments": {"path": "."}}

            if any(term in text for term in ["看", "读取", "打开", "内容", "read"]):
                return {
                    "tool": "sandbox_read_file" if target_path else "sandbox_list_files",
                    "arguments": {"path": target_path or "."},
                }

        if (
            len(executed_tools) == 1
            and executed_tools[0].get("tool") == "sandbox_list_files"
            and self._is_sandbox_write_request(user_message)
        ):
            inferred_path = self._infer_single_code_file(executed_tools[0].get("result") or {})
            if inferred_path:
                return {"tool": "sandbox_read_file", "arguments": {"path": inferred_path}}

        return None

    def _annotate_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        executed_tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if tool_name != "sandbox_write_file" or not result.get("ok"):
            return result

        path = str(arguments.get("path") or "")
        new_content = arguments.get("content")
        previous_content = None
        for item in reversed(executed_tools):
            if item.get("tool") != "sandbox_read_file":
                continue
            nested = item.get("result", {}).get("result", {})
            if nested.get("path") == path:
                previous_content = nested.get("content")
                break

        if isinstance(new_content, str) and isinstance(previous_content, str):
            result = dict(result)
            nested = dict(result.get("result") or {})
            nested["changed"] = new_content != previous_content
            if new_content == previous_content:
                nested["warning"] = "sandbox_write_file wrote identical content; no effective code change was made"
            result["result"] = nested
        return result

    def _extract_sandbox_path(
        self,
        user_message: str,
        messages: list[dict[str, Any]],
        executed_tools: list[dict[str, Any]],
    ) -> str | None:
        candidates = [user_message]
        for message in reversed(messages[-8:]):
            content = message.get("content")
            if isinstance(content, str):
                candidates.append(content)

        pattern = r"[\w.\-/\\\u4e00-\u9fff]+(?:\.py|\.js|\.ts|\.tsx|\.jsx|\.html|\.css|\.json|\.md|\.txt|\.rs|\.go)"
        for text in candidates:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(0).replace("\\", "/").strip("./")

        for item in reversed(executed_tools):
            result = item.get("result") or {}
            nested = result.get("result") if isinstance(result, dict) else None
            if isinstance(nested, dict):
                path = nested.get("path")
                if isinstance(path, str) and path:
                    return path
        return None

    def _infer_single_code_file(self, result: dict[str, Any]) -> str | None:
        nested = result.get("result") if isinstance(result, dict) else None
        items = nested.get("items") if isinstance(nested, dict) else None
        if not isinstance(items, list):
            return None
        code_files = [
            item.get("name")
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "file"
            and str(item.get("name", "")).lower().endswith((".py", ".js", ".ts", ".html", ".css", ".rs", ".go"))
        ]
        return code_files[0] if len(code_files) == 1 else None

    def _command_for_path(self, path: str) -> str:
        lower = path.lower()
        if lower.endswith(".py"):
            return f"python {path}"
        if lower.endswith((".js", ".mjs", ".cjs")):
            return f"node {path}"
        return f"python {path}"

    def _extract_code_from_text(self, text: str) -> str | None:
        fenced = re.search(r"```(?:\w+)?\s*(.*?)```", text, flags=re.S)
        code = fenced.group(1).strip() if fenced else text.strip()
        if len(code) < 20:
            return None
        code_markers = ["def ", "class ", "import ", "from ", "function ", "const ", "let ", "var ", "#include", "fn main"]
        return code if any(marker in code for marker in code_markers) else None

    def _extract_todo_text(self, user_message: str) -> str:
        text = user_message.strip()
        text = re.sub(r"^(帮我|请|给我|麻烦)?(添加|新增|创建|记个|记一个|加入)?(一个|一条)?待办(事项)?[:：，,\s]*", "", text)
        text = re.sub(r"(到|进)?(日程|待办)(界面|里|中)?", "", text)
        return text.strip(" ：:，,。")[:120]

    def _extract_memory_note_content(self, user_message: str) -> str:
        text = user_message.strip()
        text = re.sub(r"^(帮我|请|给我|麻烦)?(记住|记一下|添加记忆|新增记忆|保存记忆|记录记忆)[:：，,\s]*", "", text)
        text = re.sub(r"(到|进)?(日程)?记忆(库|功能|页面|里|中)?", "", text)
        return text.strip(" ：:，,。")[:300]

    def _extract_recent_tool_results(self, messages: list[dict[str, Any]]) -> str:
        results: list[str] = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and content.startswith("[Internal tool result"):
                results.append(content[: self.max_result_chars])
        return "\n\n".join(results[-2:])

    def _extract_recent_dialogue(self, messages: list[dict[str, Any]]) -> str:
        snippets: list[str] = []
        for message in messages[-12:]:
            content = message.get("content")
            role = message.get("role", "unknown")
            if not isinstance(content, str):
                continue
            if content.startswith("[Internal tool result"):
                continue
            content = re.sub(r"\s+", " ", content).strip()
            if content:
                snippets.append(f"{role}: {content[:800]}")
        return "\n".join(snippets[-8:]) or "None"

    def _recent_dialogue_has_edit_intent(self, messages: list[dict[str, Any]]) -> bool:
        dialogue = self._extract_recent_dialogue(messages).lower()
        terms = ["高分", "最高分", "暂停", "特殊食物", "皮肤", "颜色", "加速", "减速", "score", "pause", "highscore", "bonus"]
        return any(term in dialogue for term in terms)

    def _parse_json(self, raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"```$", "", text).strip()

        candidates = [text]
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            candidates.append(match.group(0))

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None
