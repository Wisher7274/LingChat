<template>
  <section class="code-assistant-panel" :class="{ 'is-collapsed': isCollapsed }" aria-label="Code 模式">
    <header class="code-assistant-header">
      <div v-if="!isCollapsed" class="code-assistant-title">
        <span>Code 模式</span>
        <span class="code-beta">Beta</span>
      </div>
      <button
        class="icon-button collapse-button"
        type="button"
        :title="isCollapsed ? '展开 Code 模式' : '收起侧边栏'"
        @click="toggleCollapsed"
      >
        <ChevronsLeft v-if="isCollapsed" :size="18" />
        <ChevronsRight v-else :size="18" />
      </button>
    </header>

    <div v-if="!isCollapsed" ref="messagesRef" class="code-assistant-body">
      <div v-if="messages.length === 0" class="assistant-message">
        <div class="assistant-avatar"><Bot :size="18" /></div>
        <div class="assistant-bubble">
          <div class="assistant-text">晚上好，主人！</div>
          <div class="assistant-text muted">Code 模式已准备好，可以问我代码问题，或者让我继续执行任务。</div>
        </div>
      </div>

      <article
        v-for="(message, index) in messages"
        :key="messageKey(message, index)"
        :class="message.type === 'message' ? 'user-message' : 'assistant-message'"
      >
        <template v-if="message.type === 'reply'">
          <div class="assistant-avatar"><Bot :size="18" /></div>
          <div class="assistant-bubble">
            <div class="assistant-text">{{ messageText(message, index) }}</div>
            <time v-if="message.timestamp" class="message-time">{{ formatTime(message.timestamp) }}</time>
          </div>
        </template>

        <template v-else>
          <div class="user-bubble">
            <div>{{ message.content }}</div>
            <time v-if="message.timestamp" class="message-time">{{ formatTime(message.timestamp) }}</time>
          </div>
        </template>
      </article>

      <article
        v-for="tool in recentToolLogs"
        :key="toolKey(tool)"
        class="assistant-message tool-event-message"
      >
        <div class="assistant-avatar tool-avatar">
          <LoaderCircle v-if="tool.status === 'running'" :size="16" class="animate-spin" />
          <CheckCircle2 v-else-if="tool.ok !== false && tool.status !== 'error'" :size="16" />
          <Wrench v-else :size="16" />
        </div>
        <div class="assistant-bubble tool-event-bubble" :class="toolStatusClass(tool)">
          <div class="tool-event-title">{{ toolSummary(tool) }}</div>
          <div class="tool-event-meta">
            <span>{{ tool.tool }}</span>
            <span>{{ toolStatusText(tool) }}</span>
            <span>{{ formatToolTime(tool.timestamp) }}</span>
          </div>
          <div v-if="toolDetail(tool)" class="tool-event-detail">{{ toolDetail(tool) }}</div>
        </div>
      </article>

      <article v-if="showToolBubble" class="assistant-message">
        <div class="assistant-avatar"><Wrench :size="17" /></div>
        <div class="assistant-bubble tool-bubble">
          <div class="tool-status">
            <LoaderCircle v-if="gameStore.currentStatus === 'thinking'" :size="14" class="animate-spin" />
            <CheckCircle2 v-else :size="14" />
            <span>{{ toolBubbleText }}</span>
          </div>
          <div v-if="latestToolPreview" class="tool-preview">{{ latestToolPreview }}</div>
        </div>
      </article>
    </div>

    <div v-if="!isCollapsed" class="quick-actions">
      <button type="button" @click="appendLocalSummary">
        <ClipboardList :size="14" />
        总结
      </button>
      <button type="button" @click="quickSend('继续执行当前计划')">
        <Sparkles :size="14" />
        继续
      </button>
      <button type="button" @click="appendToolSummary">
        <Wrench :size="14" />
        工具
      </button>
      <button type="button" @click="quickSend('生成一个简短计划')">
        <ListChecks :size="14" />
        计划
      </button>
    </div>

    <form v-if="!isCollapsed" class="code-composer" @submit.prevent="send">
      <textarea
        ref="inputRef"
        v-model="inputMessage"
        rows="2"
        class="code-input"
        placeholder="向 Code 模式提问..."
        :readonly="!canSend"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <div class="composer-footer">
        <div class="composer-tools">
          <button
            type="button"
            :title="gameStore.command === 'touch' ? '退出触摸模式' : '触摸模式'"
            :class="{ 'tool-enabled': gameStore.command === 'touch' }"
            @click="toggleTouchMode"
            @contextmenu.prevent="exitTouchMode"
          >
            <Hand :size="15" />
          </button>
          <button type="button" title="查看最近工具结果" @click="appendToolSummary"><Wrench :size="15" /></button>
          <button type="button" title="Code TTS 设置" @click="openTextSettings"><Volume2 :size="15" /></button>
        </div>
        <button class="send-button" type="submit" :disabled="!canSubmit" title="发送">
          <SendHorizontal :size="18" />
        </button>
      </div>
      <div class="model-line">模型：{{ currentModelLabel }} · {{ statusText }}</div>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Bot,
  CheckCircle2,
  ChevronsLeft,
  ChevronsRight,
  ClipboardList,
  Hand,
  ListChecks,
  LoaderCircle,
  SendHorizontal,
  Sparkles,
  Volume2,
  Wrench,
} from 'lucide-vue-next'
import { useGameStore } from '../../../stores/modules/game'
import { useUIStore } from '../../../stores/modules/ui/ui'
import { useSettingsStore } from '../../../stores/modules/settings'
import { scriptHandler } from '../../../api/websocket/handlers/script-handler'
import { eventQueue } from '../../../core/events/event-queue'
import type { GameMessage } from '../../../stores/modules/game/state'
import type { ToolCallLog } from '../../../stores/modules/ui/ui'
import http from '@/api/http'

const inputMessage = ref('')
const isCollapsed = ref(false)
const inputRef = ref<HTMLTextAreaElement | null>(null)
const messagesRef = ref<HTMLElement | null>(null)
const typingKey = ref('')
const typedText = ref('')
const currentModelLabel = ref('加载中')
let typingTimer: number | null = null

const gameStore = useGameStore()
const uiStore = useUIStore()
const settingsStore = useSettingsStore()

// 切换模式时管理游戏状态
watch(
  () => settingsStore.codeMode,
  async (isCodeMode) => {
    if (isCodeMode) {
      // 切换到 Code 模式时，让 Code 输入框获得焦点
      await nextTick()
      inputRef.value?.focus()
    } else {
      // 切换回 Chat 模式时：
      // 1. 清空 Code 面板自己的输入
      // 2. 不主动重置游戏状态或事件队列 - 避免吞掉 Chat 模式正在等待继续的对白
      console.log('[模式切换] 从 Code 切回 Chat，当前状态:', gameStore.currentStatus)
      inputMessage.value = ''
    }
  },
)

const autoHiddenToolIds = ref<Set<string>>(new Set())
const autoHideTimers = ref<Map<string, number>>(new Map())

watch(
  () => uiStore.toolCallLogs,
  (logs) => {
    for (const tool of logs) {
      const toolId = tool.id
      if (!toolId) continue
      if (tool.status === 'running') {
        if (autoHideTimers.value.has(toolId)) {
          clearTimeout(autoHideTimers.value.get(toolId)!)
          autoHideTimers.value.delete(toolId)
        }
        autoHiddenToolIds.value.delete(toolId)
        continue
      }
      if (!autoHideTimers.value.has(toolId) && !autoHiddenToolIds.value.has(toolId)) {
        const timer = window.setTimeout(() => {
          autoHiddenToolIds.value.add(toolId)
          autoHideTimers.value.delete(toolId)
        }, 5000)
        autoHideTimers.value.set(toolId, timer)
      }
    }
  },
  { deep: true, immediate: true },
)

const messages = computed<GameMessage[]>(() => (gameStore.dialogHistory || []).slice(-40))
const latestTool = computed(() => (uiStore.toolCallLogs || [])[0])
const recentToolLogs = computed<ToolCallLog[]>(() =>
  (uiStore.toolCallLogs || [])
    .filter((t) => t.id && !autoHiddenToolIds.value.has(t.id))
    .slice(0, 8)
    .slice()
    .reverse(),
)
const showToolBubble = computed(
  () =>
    (gameStore.currentStatus === 'thinking' || !!uiStore.activeToolStatusText) &&
    recentToolLogs.value.length === 0,
)
const toolBubbleText = computed(() => {
  if (uiStore.activeToolStatusText) return uiStore.activeToolStatusText
  if (gameStore.currentStatus === 'thinking') return '正在思考和执行...'
  return '工具执行完成'
})
const latestToolPreview = computed(() => latestTool.value?.preview || '')
const canSend = computed(() => gameStore.currentStatus === 'input')
const canSubmit = computed(() => canSend.value && inputMessage.value.trim().length > 0)
const statusText = computed(() => (gameStore.currentStatus === 'thinking' ? '思考中' : '就绪'))

const loadCurrentModel = async () => {
  try {
    const res = await http.get('/v1/chat/info/llm_model', { silent: true })
    const data = res?.data || res || {}
    const provider = data.provider || 'unknown'
    const model = data.model || 'unknown'
    currentModelLabel.value = `${provider} / ${model}`
  } catch {
    currentModelLabel.value = '未知模型'
  }
}

const compactText = (text: string, maxLength = 180) => {
  const normalized = text.replace(/\s+/g, ' ').trim()
  return normalized.length > maxLength ? `${normalized.slice(0, maxLength)}...` : normalized
}

const toolStatusLabel = (status?: string, ok?: boolean | null) => {
  if (status === 'running') return '进行中'
  if (status === 'error' || ok === false) return '失败'
  if (status === 'success' || ok === true) return '已完成'
  return status || '未知'
}

const appendAssistantNote = (content: string) => {
  gameStore.appendGameMessage({
    type: 'reply',
    displayName: 'Code Agent',
    content,
    isFinal: true,
  })
  scrollToBottom()
}

const appendLocalSummary = () => {
  const tool = latestTool.value
  const lastReply = latestReplyContent.value
  const completedByTool = tool && (tool.status === 'success' || tool.ok === true)
  const completedByReply = /完成|成功|已清空|已删除|已写入|执行完成|已修复/.test(lastReply)
  const status =
    gameStore.currentStatus === 'thinking' ? '进行中' : completedByTool || completedByReply ? '已完成' : '没有正在运行的任务'
  const lines = [`当前代码任务状态：${status}。`]

  if (tool) {
    lines.push(`最近工具：${tool.tool}（${toolStatusLabel(tool.status, tool.ok)}）。`)
    if (tool.preview) lines.push(`工具结果：${compactText(tool.preview)}`)
  }
  if (lastReply) {
    lines.push(`最近回复：${compactText(lastReply)}`)
  }
  if (!tool && !lastReply) {
    lines.push('还没有可总结的 Code 模式记录。')
  }

  appendAssistantNote(lines.join('\n'))
}

const appendToolSummary = () => {
  const tool = latestTool.value
  if (!tool) {
    appendAssistantNote('当前还没有工具执行记录。')
    return
  }

  const lines = [`最近工具：${tool.tool}`, `状态：${toolStatusLabel(tool.status, tool.ok)}`]
  if (tool.preview) lines.push(`结果：${compactText(tool.preview, 260)}`)
  appendAssistantNote(lines.join('\n'))
}

const messageKey = (message: GameMessage, index: number) => {
  return `${message.timestamp || index}-${message.type}-${message.displayName}-${message.content.slice(0, 16)}`
}

const latestReplyKey = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index--) {
    const message = messages.value[index]
    if (!message) continue
    if (message.type === 'reply') return messageKey(message, index)
  }
  return ''
})

const latestReplyContent = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index--) {
    const message = messages.value[index]
    if (!message) continue
    if (message.type === 'reply') return message.content || ''
  }
  return ''
})

const messageText = (message: GameMessage, index: number) => {
  const key = messageKey(message, index)
  return key === typingKey.value ? typedText.value : message.content
}

const formatTime = (timestamp: number) => {
  return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

const formatToolTime = (timestamp: string) => {
  const date = timestamp ? new Date(timestamp) : new Date()
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

const toolKey = (tool: ToolCallLog) => `${tool.id || tool.timestamp}-${tool.tool}-${tool.status}`

const parseToolPreview = (tool: ToolCallLog): any => {
  if (!tool.preview) return null
  try {
    return JSON.parse(tool.preview)
  } catch {
    return null
  }
}

const nestedToolResult = (tool: ToolCallLog): any => {
  const parsed = parseToolPreview(tool)
  if (!parsed || typeof parsed !== 'object') return null
  return parsed.result && typeof parsed.result === 'object' ? parsed.result : parsed
}

const toolSummary = (tool: ToolCallLog) => {
  if (tool.summary) return tool.summary
  if (tool.status === 'running') return `正在执行 ${tool.tool}`
  if (tool.status === 'error' || tool.ok === false) return `${tool.tool} 执行失败`
  return `${tool.tool} 执行完成`
}

const toolStatusText = (tool: ToolCallLog) => {
  if (tool.status === 'running') return '运行中'
  if (tool.status === 'error' || tool.ok === false) return '失败'
  return '完成'
}

const toolStatusClass = (tool: ToolCallLog) => ({
  'tool-running': tool.status === 'running',
  'tool-success': tool.status !== 'running' && tool.ok !== false && tool.status !== 'error',
  'tool-error': tool.status === 'error' || tool.ok === false,
})

const toolDetail = (tool: ToolCallLog) => {
  const result = nestedToolResult(tool)
  if (!result || typeof result !== 'object') return ''
  if (tool.tool === 'sandbox_write_file') {
    const parts = []
    if (result.line_count !== undefined) parts.push(`${result.line_count} 行`)
    if (result.bytes !== undefined) parts.push(`${result.bytes} bytes`)
    if (result.syntax_check?.ok) parts.push('语法检查通过')
    if (result.incomplete_warning) parts.push('可能未写完整')
    return parts.join(' · ')
  }
  if (tool.tool === 'sandbox_execute_command') {
    const output = String(result.stdout || result.stderr || '').trim()
    return output.length > 180 ? `${output.slice(0, 180)}...` : output
  }
  if (tool.tool === 'sandbox_list_files' && Array.isArray(result.items)) {
    return result.items
      .slice(0, 4)
      .map((item: any) => item.name)
      .join(' · ')
  }
  return ''
}

const stopTyping = () => {
  if (typingTimer !== null) {
    window.clearInterval(typingTimer)
    typingTimer = null
  }
}

const startTyping = (key: string, content: string) => {
  stopTyping()
  typingKey.value = key
  typedText.value = ''
  let cursor = 0
  const step = () => {
    cursor += content.charCodeAt(cursor) > 255 ? 1 : 2
    typedText.value = content.slice(0, cursor)
    scrollToBottom()
    if (cursor >= content.length) {
      stopTyping()
      typedText.value = content
    }
  }
  typingTimer = window.setInterval(step, 18)
}

const scrollToBottom = () => {
  // 使用多重延迟确保 DOM 和图片都渲染完成后再滚动
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (messagesRef.value) {
        messagesRef.value.scrollTo({
          top: messagesRef.value.scrollHeight,
          behavior: 'smooth',
        })
      }
    })
  })
}

watch(
  () => latestReplyKey.value,
  (key, oldKey) => {
    if (key && key !== oldKey) {
      const content = latestReplyContent.value
      startTyping(key, content)
    }
  },
)

watch(
  [() => messages.value.length, () => gameStore.currentStatus, () => uiStore.activeToolStatusText],
  scrollToBottom,
  { immediate: true },
)

onMounted(loadCurrentModel)

onBeforeUnmount(() => {
  stopTyping()
  exitTouchMode()
  for (const timer of autoHideTimers.value.values()) {
    clearTimeout(timer)
  }
  autoHideTimers.value.clear()
})

const handleRightClick = (e: MouseEvent) => {
  if (gameStore.command === 'touch') {
    e.preventDefault()
    exitTouchMode()
  }
}

const toggleTouchMode = () => {
  if (gameStore.command === 'touch') {
    exitTouchMode()
    return
  }

  document.body.style.cursor = `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' class='lucide lucide-hand-icon lucide-hand'%3E%3Cpath d='M18 11V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2'/%3E%3Cpath d='M14 10V4a2 2 0 0 0-2-2a2 2 0 0 0-2 2v2'/%3E%3Cpath d='M10 10.5V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2v8'/%3E%3Cpath d='M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15'/%3E%3C/svg%3E") 0 0, auto`
  gameStore.command = 'touch'
  document.addEventListener('contextmenu', handleRightClick)
}

const exitTouchMode = () => {
  document.body.style.cursor = 'default'
  if (gameStore.command === 'touch') {
    gameStore.command = null
  }
  document.removeEventListener('contextmenu', handleRightClick)
}

const openTextSettings = () => {
  uiStore.toggleSettings(true)
  uiStore.setSettingsTab('text')
}

const toggleCollapsed = () => {
  isCollapsed.value = !isCollapsed.value
  if (!isCollapsed.value) {
    scrollToBottom()
  }
}

const quickSend = (text: string) => {
  if (gameStore.currentStatus !== 'input') return
  inputMessage.value = text
  send()
}

function send() {
  if (gameStore.currentStatus === 'responding') {
    eventQueue.continue()
    return
  }

  if (!canSubmit.value) return
  const text = inputMessage.value.trim()
  gameStore.appendGameMessage({
    type: 'message',
    displayName: gameStore.userName || 'You',
    content: text,
  })
  inputMessage.value = ''
  gameStore.currentStatus = 'thinking'
  scriptHandler.sendMessage(text)
  scrollToBottom()
  nextTick(() => inputRef.value?.focus())
}
</script>

<style scoped>
.code-assistant-panel {
  position: fixed;
  top: 4.25rem;
  right: 1.5rem;
  bottom: 1rem;
  z-index: 1;
  display: flex;
  width: min(27.5rem, calc(100vw - 3rem));
  min-height: 34rem;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(125, 211, 252, 0.3);
  border-radius: 22px;
  background:
    linear-gradient(145deg, rgba(15, 31, 57, 0.78), rgba(10, 24, 46, 0.88)),
    rgba(255, 255, 255, 0.08);
  box-shadow:
    0 22px 75px rgba(0, 0, 0, 0.42),
    inset 0 1px 1px rgba(255, 255, 255, 0.14);
  color: white;
  text-shadow: none;
  backdrop-filter: blur(32px) saturate(180%);
  -webkit-backdrop-filter: blur(32px) saturate(180%);
  transition:
    transform 720ms cubic-bezier(0.22, 0.8, 0.22, 1),
    opacity 720ms ease,
    box-shadow 720ms ease;
}

.code-assistant-panel.is-collapsed {
  opacity: 0.16;
  transform: translateX(calc(100% - 1.65rem));
  box-shadow: none;
}

.code-assistant-panel.is-collapsed:hover {
  opacity: 0.68;
  transform: translateX(calc(100% - 3.1rem));
}

.code-assistant-header {
  display: flex;
  min-height: 3.5rem;
  align-items: center;
  justify-content: space-between;
  padding: 0 1rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.055);
}

.code-assistant-panel.is-collapsed .code-assistant-header {
  justify-content: flex-start;
  padding: 0 0.35rem;
  border-bottom: 0;
}

.code-assistant-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: rgb(248, 250, 252);
  font-size: 1rem;
  font-weight: 800;
}

.code-beta {
  border: 1px solid rgba(125, 211, 252, 0.4);
  border-radius: 999px;
  background: rgba(14, 165, 233, 0.18);
  padding: 0.1rem 0.45rem;
  color: rgb(186, 230, 253);
  font-size: 0.6875rem;
}

.icon-button {
  display: grid;
  width: 2rem;
  height: 2rem;
  place-items: center;
  border-radius: 10px;
  color: rgba(224, 242, 254, 0.75);
}

.collapse-button {
  position: absolute;
  top: 0.75rem;
  right: 0.875rem;
  border: 1px solid rgba(125, 211, 252, 0.22);
  background: rgba(255, 255, 255, 0.08);
  z-index: 2;
}

.code-assistant-panel.is-collapsed .collapse-button {
  left: 0.35rem;
  right: auto;
}

.icon-button:hover {
  background: rgba(255, 255, 255, 0.12);
  color: white;
}

.code-assistant-body {
  flex: 1;
  overflow-y: auto;
  padding: 0.95rem;
  scrollbar-color: rgba(125, 211, 252, 0.62) transparent;
  scrollbar-width: thin;
}

.assistant-message,
.user-message {
  display: flex;
  gap: 0.55rem;
  margin-bottom: 0.85rem;
}

.assistant-avatar {
  display: grid;
  width: 1.75rem;
  height: 1.75rem;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid rgba(125, 211, 252, 0.38);
  border-radius: 50%;
  background: rgba(14, 165, 233, 0.18);
  color: rgb(224, 242, 254);
  box-shadow: 0 0 18px rgba(14, 165, 233, 0.16);
}

.assistant-bubble,
.user-bubble {
  max-width: 82%;
  border: 1px solid rgba(255, 255, 255, 0.13);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
}

.assistant-bubble {
  border-radius: 14px 14px 14px 4px;
  background: rgba(255, 255, 255, 0.14);
  padding: 0.65rem 0.75rem;
  color: rgba(248, 250, 252, 0.96);
}

.assistant-text {
  font-size: 0.875rem;
  line-height: 1.6;
}

.assistant-text.muted {
  margin-top: 0.25rem;
  color: rgba(226, 232, 240, 0.72);
}

.user-message {
  justify-content: flex-end;
}

.user-bubble {
  border-radius: 14px 14px 4px 14px;
  background: rgba(14, 116, 205, 0.58);
  padding: 0.62rem 0.72rem;
  color: white;
  font-size: 0.875rem;
  line-height: 1.55;
  box-shadow: 0 10px 24px rgba(14, 116, 205, 0.18);
}

.message-time {
  display: block;
  margin-top: 0.3rem;
  color: rgba(226, 232, 240, 0.55);
  font-size: 0.6875rem;
  text-align: right;
}

.tool-bubble {
  max-width: 90%;
}

.tool-event-message {
  margin-left: 0.2rem;
}

.tool-avatar {
  border-color: rgba(56, 189, 248, 0.42);
  background: rgba(2, 132, 199, 0.18);
}

.tool-event-bubble {
  width: min(24rem, 88%);
  max-width: 88%;
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.55);
  padding: 0.62rem 0.7rem;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}

.tool-event-bubble.tool-running {
  border-color: rgba(125, 211, 252, 0.32);
}

.tool-event-bubble.tool-success {
  border-color: rgba(74, 222, 128, 0.26);
}

.tool-event-bubble.tool-error {
  border-color: rgba(248, 113, 113, 0.36);
}

.tool-event-title {
  color: rgba(248, 250, 252, 0.94);
  font-size: 0.8125rem;
  font-weight: 800;
  line-height: 1.45;
}

.tool-event-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.38rem;
  margin-top: 0.28rem;
  color: rgba(186, 230, 253, 0.62);
  font-family:
    ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
  font-size: 0.66rem;
}

.tool-event-meta span:not(:last-child)::after {
  content: '·';
  margin-left: 0.38rem;
  color: rgba(148, 163, 184, 0.5);
}

.tool-event-detail {
  margin-top: 0.38rem;
  color: rgba(203, 213, 225, 0.72);
  font-size: 0.72rem;
  line-height: 1.45;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.tool-status {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  color: rgb(186, 230, 253);
  font-size: 0.8125rem;
  font-weight: 700;
}

.tool-preview {
  margin-top: 0.35rem;
  color: rgba(226, 232, 240, 0.62);
  font-size: 0.75rem;
  line-height: 1.45;
}

.quick-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  padding: 0.2rem 0.95rem 0.65rem;
}

.quick-actions button {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid rgba(125, 211, 252, 0.2);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.1);
  padding: 0.45rem 0.62rem;
  color: rgb(224, 242, 254);
  font-size: 0.75rem;
}

.quick-actions button:hover {
  background: rgba(125, 211, 252, 0.18);
}

.code-composer {
  margin: 0 0.95rem 0.95rem;
  border: 1px solid rgba(125, 211, 252, 0.45);
  border-radius: 16px;
  background: rgba(15, 23, 42, 0.65);
  box-shadow:
    0 12px 34px rgba(0, 0, 0, 0.35),
    inset 0 1px 1px rgba(255, 255, 255, 0.15);
}

.code-input {
  display: block;
  width: 100%;
  min-height: 4rem;
  max-height: 9rem;
  resize: none;
  border: 0;
  background: transparent;
  padding: 0.75rem 0.8rem 0.35rem;
  color: rgb(248, 250, 252);
  font-size: 0.875rem;
  line-height: 1.5;
  outline: none;
  text-shadow: none;
}

.code-input::placeholder {
  color: rgba(203, 213, 225, 0.52);
}

.composer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.35rem 0.55rem 0.55rem;
}

.composer-tools {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.composer-tools button {
  display: grid;
  width: 1.75rem;
  height: 1.75rem;
  place-items: center;
  border-radius: 9px;
  color: rgba(224, 242, 254, 0.72);
}

.composer-tools button:hover {
  background: rgba(255, 255, 255, 0.12);
  color: white;
}

.composer-tools button.tool-enabled {
  background: rgba(125, 211, 252, 0.2);
  color: rgb(186, 230, 253);
  box-shadow: inset 0 0 0 1px rgba(125, 211, 252, 0.28);
}

.send-button {
  display: grid;
  width: 2.2rem;
  height: 2.2rem;
  place-items: center;
  border-radius: 50%;
  background: rgb(59, 130, 246);
  color: white;
  box-shadow: 0 0 22px rgba(59, 130, 246, 0.35);
}

.send-button:hover:not(:disabled) {
  background: rgb(14, 165, 233);
}

.send-button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.model-line {
  padding: 0 0.72rem 0.58rem;
  color: rgba(203, 213, 225, 0.62);
  font-size: 0.6875rem;
}

@media (max-width: 720px) {
  .code-assistant-panel {
    left: 0.75rem;
    right: 0.75rem;
    top: 5.25rem;
    bottom: 0.75rem;
    width: auto;
    min-height: 0;
  }
}
</style>
