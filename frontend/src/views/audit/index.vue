<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>操作审计管理</h2>
        <p class="page-desc">维护操作审计日志的输入、执行约束与结果记录，为操作审计提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记审计日志</button>
        <button class="btn" type="button" @click="exportRows">导出操作审计清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section v-if="notifications.length" class="notice-box">
      <h3 class="notice-title">质量通知（待审核真实异常，与概览异常量同口径）</h3>
      <ul class="notice-list">
        <li v-for="note in notifications" :key="note.id">
          <span class="status-badge" :data-status="note.状态">{{ note.状态 }}</span>
          {{ note.日志编号 }} · {{ note.操作类型 }} · {{ note.操作对象 }} · 影响行数 {{ note.影响行数 }}
        </li>
      </ul>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>异常标记</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-terminal="row.terminal ? 'true' : 'false'">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-badge" :data-status="row.status">{{ row.status }}</span>
            <span v-if="row.terminal" class="terminal-flag">终态</span>
          </td>
          <td>
            <span :class="row.abnormal ? 'flag-abnormal' : 'flag-normal'">
              {{ row.abnormal ? '真实异常' : '非异常' }}
            </span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action.name"
              class="link"
              type="button"
              @click="runAction(action.name, row)"
            >
              {{ action.name }}
            </button>
            <span v-if="!availableActions(row).length" class="muted-text">无可用动作</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无操作审计数据，可先登记审计日志</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条操作审计记录</span>
      <span v-if="successMessage" class="success-text">{{ successMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type Snapshot = {
  total: number
  pending: number
  abnormal: number
  ignored: number
  archived: number
  affected_rows: number
  notifications: { id: number; 日志编号: string; 操作类型: string; 操作对象: string; 状态: string; 影响行数: number }[]
}
type ActionReply = {
  ok: boolean
  message: string
  applied?: boolean
  idempotent?: boolean
  entry?: Row | null
  snapshot?: Snapshot
}

const ENDPOINT = '/api/audit'
const columns = ['日志编号', '操作类型', '操作对象', '操作详情', '操作来源', '执行时间', '影响行数', '回滚能力']
const statuses = ['草稿', '待审核', '已归档', '已忽略']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)
const snapshot = ref<Snapshot>({
  total: 0,
  pending: 0,
  abnormal: 0,
  ignored: 0,
  archived: 0,
  affected_rows: 0,
  notifications: [],
})

const stats = computed(() => [
  { label: '审计日志总数', value: snapshot.value.total },
  { label: '待审核日志', value: snapshot.value.pending },
  { label: '真实异常量', value: snapshot.value.abnormal },
  { label: '已忽略日志', value: snapshot.value.ignored },
  { label: '已归档日志', value: snapshot.value.archived },
  { label: '审计影响行数', value: snapshot.value.affected_rows },
])

const notifications = computed(() => snapshot.value.notifications)

// 只暴露当前状态下合法的动作，从源头避免非法状态跳转；恢复仅对已忽略开放
function availableActions(row: Row): { name: string }[] {
  switch (row.status) {
    case '草稿':
      return [{ name: '提交审核' }]
    case '待审核':
      return [{ name: '归档日志' }, { name: '忽略日志' }]
    case '已忽略':
      return [{ name: '恢复日志' }]
    default:
      return []
  }
}

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '审计日志登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = (await response.json()) as ActionReply
    if (!response.ok || !payload.ok) {
      // 后端会明确给出当前状态不允许的原因
      errorMessage.value = payload.message || '操作审计动作未生效，请稍后重试'
      await reload()
      return
    }
    if (payload.snapshot) {
      snapshot.value = payload.snapshot
    }
    successMessage.value = payload.idempotent
      ? `${payload.message}（状态保持已忽略，异常量未变化）`
      : payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作审计操作失败'
    await reload()
  }
}

async function loadSnapshot() {
  try {
    const response = await request(`${ENDPOINT}/snapshot`)
    if (response.ok) {
      snapshot.value = (await response.json()) as Snapshot
    }
  } catch {
    // 快照失败不阻断列表读取，下次动作回执会重新带回统一快照
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value) query.set(key, value)
  })
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('审计日志列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadSnapshot()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作审计列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.notice-box {
  background: #fff;
  border: 1px solid var(--border);
  border-left: 4px solid #d92d20;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.notice-title { font-size: 13px; margin: 0 0 6px; }
.notice-list { margin: 0; padding-left: 4px; list-style: none; display: grid; gap: 4px; font-size: 13px; }
.status-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--border);
}
.status-badge[data-status='待审核'] { background: #fffaeb; color: #b54708; border-color: #fedf89; }
.status-badge[data-status='已忽略'] { background: #f2f4f7; color: #475467; border-color: #eaecf0; }
.status-badge[data-status='已归档'] { background: #ecfdf3; color: #027a48; border-color: #abefc6; }
.status-badge[data-status='草稿'] { background: #eff8ff; color: #175cd3; border-color: #b2ddff; }
.terminal-flag { margin-left: 6px; font-size: 12px; color: var(--muted); }
.flag-abnormal { color: #b42318; font-weight: 600; }
.flag-normal { color: #027a48; }
.muted-text { color: var(--muted); font-size: 12px; }
.success-text { color: #027a48; }
.filter-item select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
</style>
