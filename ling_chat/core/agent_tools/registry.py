import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from datetime import datetime

from ling_chat.core.agent_tools.sandbox import (
    sandbox_delete_file,
    sandbox_execute_command,
    sandbox_list_files,
    sandbox_read_file,
    sandbox_write_file,
)
from ling_chat.core.ai_service.game_system.game_status import GameStatus
from ling_chat.game_database.managers.role_manager import RoleManager
from ling_chat.utils.runtime_path import user_data_path
from ling_chat.utils.scene_manager import SceneManager
from ling_chat.utils.scene_utils import list_available_scenes


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


class SimpleToolRegistry:
    def __init__(self, game_status: GameStatus):
        self.game_status = game_status
        self._tools: dict[str, ToolSpec] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register(
            ToolSpec(
                name="get_current_status",
                description="Read the current LingChat runtime status, including character, scene, background, and message count.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._get_current_status,
            )
        )
        self.register(
            ToolSpec(
                name="get_current_scene",
                description="Read the current scene description and scene name that the character is in.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._get_current_scene,
            )
        )
        self.register(
            ToolSpec(
                name="get_memory",
                description=(
                    "Read ONLY the current character's automatic role memory bank, including short-term memory, "
                    "long-term memory, user info, and promises. Do not use this for manually saved memory notes "
                    "from the schedule memory panel."
                ),
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._get_memory,
            )
        )
        self.register(
            ToolSpec(
                name="get_current_time",
                description="Get the current date and time.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._get_current_time,
            )
        )
        self.register(
            ToolSpec(
                name="get_schedules",
                description=(
                    "Read schedules, todos, and important days from the local LingChat schedule data. "
                    "This may include memoryNotes, but use get_memory_notes when the user specifically asks "
                    "for the manual memory library."
                ),
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._get_schedules,
            )
        )
        self.register(
            ToolSpec(
                name="get_updated_plan",
                description=(
                    "Read LingChat's current Updated Plan from schedules.json updatedPlan. "
                    "Use this for the visible checklist-style plan, not for todos, schedules, or memory."
                ),
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._get_updated_plan,
            )
        )
        self.register(
            ToolSpec(
                name="update_plan",
                description=(
                    "Replace LingChat's current Updated Plan with a checklist of steps and statuses. "
                    "Use this when the user asks to update, create, revise, or show progress on a plan. "
                    "This writes schedules.json updatedPlan and does not create todo items."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Optional plan title."},
                        "items": {
                            "type": "array",
                            "description": "Plan steps in display order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step": {"type": "string", "description": "Plan step text."},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed", "cancelled"],
                                        "description": "Current status for this step.",
                                    },
                                    "note": {"type": "string", "description": "Optional short note."},
                                },
                                "required": ["step"],
                                "additionalProperties": False,
                            },
                        },
                        "source": {"type": "string", "description": "Optional source label. Default is AI."},
                    },
                    "required": ["items"],
                    "additionalProperties": False,
                },
                handler=self._update_plan,
            )
        )
        self.register(
            ToolSpec(
                name="schedule_add_todo",
                description="Add a todo item into the LingChat schedule/todo data so it appears in the schedule UI.",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Todo text to add."},
                        "group_title": {"type": "string", "description": "Optional todo group title. Default is AI 添加."},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Priority from 1 to 5."},
                        "deadline": {"type": "string", "description": "Optional deadline text or date."},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self._schedule_add_todo,
            )
        )
        self.register(
            ToolSpec(
                name="get_memory_notes",
                description=(
                    "Read ONLY manually saved LingChat memory notes from the schedule memory panel "
                    "(schedules.json memoryNotes). This is the same storage used by memory_add_note."
                ),
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._get_memory_notes,
            )
        )
        self.register(
            ToolSpec(
                name="memory_add_note",
                description=(
                    "Add a durable manual memory note into the schedule memory panel "
                    "(schedules.json memoryNotes). This does not update the character's automatic role memory bank."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The memory note content."},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags."},
                        "source": {"type": "string", "description": "Optional source label."},
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                handler=self._memory_add_note,
            )
        )
        self.register(
            ToolSpec(
                name="list_scenes",
                description="List available LingChat scenes and their descriptions.",
                parameters={
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                    "additionalProperties": False,
                },
                handler=self._list_scenes,
            )
        )
        self.register(
            ToolSpec(
                name="list_characters",
                description="List known LingChat characters from the local role database.",
                parameters={
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                    "additionalProperties": False,
                },
                handler=self._list_characters,
            )
        )
        self.register(
            ToolSpec(
                name="switch_scene",
                description="Switch to a different scene by scene name or scene ID. If the exact name is not found, it will try to match partially.",
                parameters={
                    "type": "object",
                    "properties": {
                        "scene_name": {"type": "string", "description": "The name or partial name of the scene to switch to."},
                    },
                    "required": ["scene_name"],
                    "additionalProperties": False,
                },
                handler=self._switch_scene,
            )
        )
        self.register(
            ToolSpec(
                name="switch_character",
                description="Switch to a different character by character name. If the exact name is not found, it will try to match partially.",
                parameters={
                    "type": "object",
                    "properties": {
                        "character_name": {"type": "string", "description": "The name or partial name of the character to switch to."},
                    },
                    "required": ["character_name"],
                    "additionalProperties": False,
                },
                handler=self._switch_character,
            )
        )
        # --- 沙盒工具 ---
        self.register(
            ToolSpec(
                name="sandbox_read_file",
                description="Read the content of a file inside the sandbox. Only files within the sandbox can be accessed.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the file inside the sandbox."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda args: sandbox_read_file(args.get("path", "")),
            )
        )
        self.register(
            ToolSpec(
                name="sandbox_write_file",
                description="Write or overwrite a file inside the sandbox. Only files within the sandbox can be modified.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the file inside the sandbox."},
                        "content": {"type": "string", "description": "Content to write to the file."},
                        "append": {"type": "boolean", "description": "If true, append to the file instead of overwriting."},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                handler=lambda args: sandbox_write_file(
                    args.get("path", ""), args.get("content", ""), args.get("append", False)
                ),
            )
        )
        self.register(
            ToolSpec(
                name="sandbox_list_files",
                description="List files and directories inside the sandbox.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the directory inside the sandbox. Default is root."},
                    },
                    "additionalProperties": False,
                },
                handler=lambda args: sandbox_list_files(args.get("path", ".")),
            )
        )
        self.register(
            ToolSpec(
                name="sandbox_delete_file",
                description="Delete a file or directory inside the sandbox. Use recursive=true for non-empty directories or path='.' to clear the sandbox contents.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the file or directory inside the sandbox."},
                        "recursive": {"type": "boolean", "description": "Delete non-empty directories recursively. Use with path='.' to clear sandbox contents."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda args: sandbox_delete_file(
                    args.get("path", ""), args.get("recursive", False)
                ),
            )
        )
        self.register(
            ToolSpec(
                name="sandbox_execute_command",
                description="Execute a safe command inside the sandbox. Only whitelisted commands are allowed (python, node, npm, git, ls, etc.). Dangerous commands are blocked.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The command to execute."},
                        "timeout": {"type": "integer", "description": "Timeout in seconds (1-120). Default is 30."},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=lambda args: sandbox_execute_command(
                    args.get("command", ""), args.get("timeout", 30)
                ),
            )
        )

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe_for_prompt(self, names: set[str] | None = None) -> str:
        selected_tools = self._tools.values()
        if names is not None:
            selected_tools = [spec for spec in selected_tools if spec.name in names]
        payload = [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in selected_tools
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        spec = self._tools.get(name)
        if not spec:
            return {
                "ok": False,
                "error": f"Unknown tool: {name}",
                "available_tools": self.names,
            }
        try:
            result = spec.handler(arguments)
            ok = bool(result.get("ok", True)) if isinstance(result, dict) else True

            # 对于写入文件操作，自动读取内容并附加到结果中，防止 LLM 幻觉
            if name == "sandbox_write_file" and ok:
                file_path = arguments.get("path", "")
                read_result = sandbox_read_file(file_path)
                if read_result.get("ok"):
                    content = read_result.get("content", "")
                    max_preview = 3000
                    if len(content) > max_preview:
                        content = content[:max_preview] + "\n...[truncated]"
                    result["content_preview"] = content

            return {"ok": ok, "tool": name, "result": result}
        except Exception as exc:
            return {"ok": False, "tool": name, "error": str(exc)}

    def _get_current_time(self, _: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now()
        return {
            "datetime": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
        }

    def _switch_scene(self, arguments: dict[str, Any]) -> dict[str, Any]:
        scene_name = str(arguments.get("scene_name", "")).strip()
        if not scene_name:
            return {"ok": False, "error": "scene_name is required"}

        scene_manager = SceneManager()
        scenes = scene_manager.list_scenes()

        # 先尝试完全匹配
        target = None
        for scene in scenes:
            if scene.get("sceneName", "").lower() == scene_name.lower():
                target = scene
                break

        # 再尝试部分匹配
        if target is None:
            for scene in scenes:
                if scene_name.lower() in scene.get("sceneName", "").lower():
                    target = scene
                    break

        # 再尝试匹配 txt 场景
        if target is None:
            for txt_scene in list_available_scenes():
                filename = str(txt_scene.get("filename", "")).replace(".txt", "")
                if scene_name.lower() in filename.lower():
                    target = {
                        "id": f"txt_{txt_scene.get('filename', '')}",
                        "sceneName": filename,
                        "sceneImage": txt_scene.get("filename"),
                        "sceneDescription": txt_scene.get("description", ""),
                        "source": "txt",
                    }
                    break

        if target is None:
            return {"ok": False, "error": f"Scene '{scene_name}' not found", "available_scenes": [s.get("sceneName") for s in scenes]}

        self.game_status.current_scene = target.get("sceneDescription", "")
        return {"ok": True, "scene": target, "message": f"Switched to scene: {target.get('sceneName')}"}

    def _switch_character(self, arguments: dict[str, Any]) -> dict[str, Any]:
        character_name = str(arguments.get("character_name", "")).strip()
        if not character_name:
            return {"ok": False, "error": "character_name is required"}

        role, settings = self._find_character_role(character_name)

        if role is None or not role.id or settings is None:
            return {"ok": False, "error": f"Character '{character_name}' not found"}

        settings.character_id = role.id

        try:
            from ling_chat.core.service_manager import service_manager
            from ling_chat.game_database.managers.user_manager import UserManager

            if service_manager.ai_service is not None:
                service_manager.ai_service.import_settings(settings=settings)
                service_manager.ai_service.reset_lines()
            else:
                game_role = self.game_status.role_manager.get_role(role.id)
                self.game_status.current_character = game_role
                self.game_status.main_role = game_role
                self.game_status.onstage_role(game_role)

            UserManager.update_last_character(user_id=1, role_id=role.id)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to switch character: {exc}"}

        game_role = self.game_status.current_character or self.game_status.role_manager.get_role(role.id)
        display_name = game_role.display_name if game_role else settings.ai_name

        return {
            "ok": True,
            "character": {
                "id": role.id,
                "role_id": role.id,
                "title": role.name,
                "name": settings.ai_name,
                "subtitle": settings.ai_subtitle,
                "folder_name": role.resource_folder,
                "resource_folder": role.resource_folder,
            },
            "message": f"Switched to character: {display_name}",
        }

    def _find_character_role(self, character_name: str):
        needle = character_name.strip()
        if not needle:
            return None, None

        role = RoleManager.get_role_by_name(needle)
        if role and role.id:
            settings = RoleManager.get_role_settings_by_id(role.id)
            if settings:
                return role, settings

        roles = RoleManager.get_all_main_roles()

        exact_matches = []
        partial_matches = []
        needle_lower = needle.lower()
        for role in roles:
            if not role.id:
                continue
            settings = RoleManager.get_role_settings_by_id(role.id)
            if settings is None:
                continue
            candidates = [
                role.name,
                role.resource_folder,
                settings.ai_name,
                settings.title,
            ]
            normalized = [str(value).strip() for value in candidates if value]
            if any(value.lower() == needle_lower for value in normalized):
                exact_matches.append((role, settings))
            elif any(needle_lower in value.lower() for value in normalized):
                partial_matches.append((role, settings))

        if exact_matches:
            return exact_matches[0]
        if partial_matches:
            return partial_matches[0]

        roles = RoleManager.search_roles_by_name(needle, limit=5)
        for role in roles:
            if role.id:
                settings = RoleManager.get_role_settings_by_id(role.id)
                if settings:
                    return role, settings

        return None, None

    def _get_current_status(self, _: dict[str, Any]) -> dict[str, Any]:
        role = self.game_status.current_character
        player = self.game_status.player
        return {
            "current_character": {
                "role_id": role.role_id if role else None,
                "display_name": role.display_name if role else None,
                "resource_path": role.resource_path if role else None,
            },
            "player": {
                "user_name": player.user_name,
                "user_subtitle": player.user_subtitle,
            },
            "scene": {
                "current_scene": self.game_status.current_scene,
                "scene_description": self.game_status.scene_description,
            },
            "media": {
                "background": self.game_status.background,
                "background_effect": self.game_status.background_effect,
                "background_music": self.game_status.background_music,
                "present_pic": self.game_status.present_pic,
            },
            "message_count": self.game_status.get_chat_message_count(),
            "active_save_id": self.game_status.active_save_id,
        }

    def _get_current_scene(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_scene": self.game_status.current_scene,
            "scene_description": self.game_status.scene_description,
        }

    def _get_memory(self, _: dict[str, Any]) -> dict[str, Any]:
        role = self.game_status.current_character
        if not role:
            return {"error": "No current character is active."}
        mb = role.memory_bank
        return {
            "character": role.display_name,
            "short_term": mb.data.short_term,
            "long_term": mb.data.long_term,
            "user_info": mb.data.user_info,
            "promises": mb.data.promises,
        }

    def _get_schedules(self, _: dict[str, Any]) -> dict[str, Any]:
        data_path = user_data_path / "game_data" / "schedules.json"
        data = self._read_json(data_path, self._default_schedule_data())
        data.setdefault("updatedPlan", None)
        return {
            "path": str(data_path),
            "summary": {
                "schedule_group_count": len(data.get("scheduleGroups", {}) or {}),
                "todo_group_count": len(data.get("todoGroups", {}) or {}),
                "important_day_count": len(data.get("importantDays", []) or []),
                "memory_note_count": len(data.get("memoryNotes", []) or []),
                "has_updated_plan": bool(data.get("updatedPlan")),
            },
            "data": data,
        }

    def _get_updated_plan(self, _: dict[str, Any]) -> dict[str, Any]:
        data_path = user_data_path / "game_data" / "schedules.json"
        data = self._read_json(data_path, self._default_schedule_data())
        plan = data.get("updatedPlan")
        return {
            "path": str(data_path),
            "plan": plan,
            "item_count": len(plan.get("items", []) or []) if isinstance(plan, dict) else 0,
        }

    def _update_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_items = arguments.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return {"ok": False, "error": "items must be a non-empty array"}

        items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            if isinstance(raw_item, dict):
                step = str(raw_item.get("step", "")).strip()
                status = str(raw_item.get("status") or "pending").strip()
                note = str(raw_item.get("note") or "").strip()
            else:
                step = str(raw_item).strip()
                status = "pending"
                note = ""

            if not step:
                continue
            if status not in {"pending", "in_progress", "completed", "cancelled"}:
                status = "pending"

            item = {"step": step, "status": status}
            if note:
                item["note"] = note
            items.append(item)

        if not items:
            return {"ok": False, "error": "at least one non-empty step is required"}

        data_path = user_data_path / "game_data" / "schedules.json"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read_json(data_path, self._default_schedule_data())
        data["updatedPlan"] = {
            "title": str(arguments.get("title") or "Updated Plan").strip() or "Updated Plan",
            "items": items,
            "source": str(arguments.get("source") or "AI").strip() or "AI",
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
        }

        with data_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        return {
            "ok": True,
            "path": str(data_path),
            "plan": data["updatedPlan"],
            "item_count": len(items),
        }

    def _schedule_add_todo(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = str(arguments.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "text is required"}

        group_title = str(arguments.get("group_title") or "AI 添加").strip() or "AI 添加"
        try:
            priority = int(arguments.get("priority", 1))
        except (TypeError, ValueError):
            priority = 1
        priority = max(1, min(priority, 5))
        deadline = str(arguments.get("deadline") or "").strip()

        data_path = user_data_path / "game_data" / "schedules.json"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read_json(data_path, self._default_schedule_data())
        todo_groups = data.setdefault("todoGroups", {})

        group_id = None
        for existing_id, group in todo_groups.items():
            if isinstance(group, dict) and group.get("title") == group_title:
                group_id = existing_id
                break

        if group_id is None:
            group_id = f"ai_{int(datetime.now().timestamp() * 1000)}"
            todo_groups[group_id] = {
                "title": group_title,
                "description": "AI 添加的待办",
                "todos": [],
            }

        todo_item = {
            "id": int(datetime.now().timestamp() * 1000),
            "text": text,
            "priority": priority,
            "completed": False,
        }
        if deadline:
            todo_item["deadline"] = deadline

        group = todo_groups[group_id]
        group.setdefault("todos", []).append(todo_item)

        with data_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        return {
            "ok": True,
            "path": str(data_path),
            "group_id": group_id,
            "group_title": group_title,
            "todo": todo_item,
        }

    def _get_memory_notes(self, _: dict[str, Any]) -> dict[str, Any]:
        data_path = user_data_path / "game_data" / "schedules.json"
        data = self._read_json(data_path, self._default_schedule_data())
        notes = data.get("memoryNotes", []) or []
        return {
            "path": str(data_path),
            "count": len(notes),
            "items": notes,
        }

    def _memory_add_note(self, arguments: dict[str, Any]) -> dict[str, Any]:
        content = str(arguments.get("content", "")).strip()
        if not content:
            return {"ok": False, "error": "content is required"}

        raw_tags = arguments.get("tags") or []
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
        source = str(arguments.get("source") or "AI").strip() or "AI"

        data_path = user_data_path / "game_data" / "schedules.json"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read_json(data_path, self._default_schedule_data())
        notes = data.setdefault("memoryNotes", [])

        note = {
            "id": f"mem_{int(datetime.now().timestamp() * 1000)}",
            "content": content,
            "tags": tags,
            "source": source,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
        }
        notes.insert(0, note)

        with data_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        return {
            "ok": True,
            "path": str(data_path),
            "memory": note,
            "count": len(notes),
        }

    def _list_scenes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = self._coerce_limit(arguments.get("limit"), 10)
        scene_manager = SceneManager()
        scenes = scene_manager.list_scenes()

        existing_images = {scene.get("sceneImage") for scene in scenes if scene.get("sceneImage")}
        for txt_scene in list_available_scenes():
            if txt_scene.get("filename") not in existing_images:
                scenes.append(
                    {
                        "id": f"txt_{txt_scene.get('filename', '')}",
                        "sceneName": str(txt_scene.get("filename", "")).replace(".txt", ""),
                        "sceneImage": txt_scene.get("filename"),
                        "sceneDescription": txt_scene.get("description", ""),
                        "source": "txt",
                    }
                )

        return {"count": len(scenes), "items": scenes[:limit]}

    def _list_characters(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = self._coerce_limit(arguments.get("limit"), 10)
        roles = RoleManager.get_all_main_roles()
        items = []
        for role in roles[:limit]:
            settings = RoleManager.get_role_settings_by_id(role.id) if role.id else None
            items.append(
                {
                    "role_id": role.id,
                    "name": settings.ai_name if settings else role.name,
                    "subtitle": settings.ai_subtitle if settings else "",
                    "resource_folder": role.resource_folder,
                    "info": settings.info if settings else "",
                }
            )
        return {"count": len(roles), "items": items}

    def _read_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return default
        for key, value in default.items():
            data.setdefault(key, value)
        return data

    def _default_schedule_data(self) -> dict[str, Any]:
        return {
            "scheduleGroups": {},
            "todoGroups": {},
            "importantDays": [],
            "memoryNotes": [],
            "updatedPlan": None,
        }

    def _coerce_limit(self, value: Any, default: int) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            limit = default
        return max(1, min(limit, 20))
