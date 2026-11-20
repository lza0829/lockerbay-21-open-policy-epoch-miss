<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const proofs = ref([])
const q = ref([])
const currentHours = ref(null)
const loadedAt = ref('')

const drift = computed(() => {
  if (currentHours.value === null) return []
  return proofs.value.filter((p) => p.overdue_hours_used !== null && p.overdue_hours_used !== currentHours.value)
})

async function load() {
  rows.value = await api.occupancies()
  q.value = await api.quarantine()
  const body = await api.deadlines()
  proofs.value = body.rows
  currentHours.value = body.current_overdue_hours
  loadedAt.value = new Date().toLocaleTimeString()
}
onMounted(load)
</script>
<template>
  <div data-plant="21-open-policy-epoch-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>回收</h1>
    <p class="sub">
      deadline 在开占用时物化落库，此处只读；柜规变更只影响之后新开的占用。
    </p>

    <div class="stats">
      <div class="stat"><b>{{ proofs.length }}</b><span>核对行</span></div>
      <div class="stat"><b>{{ proofs.filter((p) => p.deadline_missing).length }}</b><span>缺 deadline</span></div>
      <div class="stat"><b>{{ currentHours ?? '—' }}</b><span>现行 overdue_hours</span></div>
      <div class="stat"><b>{{ drift.length }}</b><span>按旧规落地</span></div>
    </div>

    <div class="panel">
      <h2 style="margin:0 0 .5rem;font-size:1rem">deadline 核对（占用 · 开启 · 截止 · 所用小时）</h2>
      <p class="sub">
        只读接口 <code>GET /api/reclaim/deadlines</code>；读取于 {{ loadedAt }}。
        有 {{ drift.length }} 笔占用按旧柜规落地，截止时刻保持原值、不随柜规重算。
      </p>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>截止</th><th>所用小时</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.occupancy_id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td><code>{{ p.reclaim_deadline_at || '缺列' }}</code></td>
            <td>{{ p.overdue_hours_used ?? '—' }}</td>
            <td>{{ p.status }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用账</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>状态</th><th>开启</th><th>截止</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td><code>{{ r.id }}</code></td>
            <td>{{ r.cell_id }}</td>
            <td>{{ r.tracking_no }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.opened_at }}</td>
            <td><code>{{ r.reclaim_deadline_at || '—' }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">搁置</h2>
      <p v-if="!q.length" class="sub">暂无搁置。</p>
      <table v-else>
        <thead><tr><th>id</th><th>原因</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="item in q" :key="item.id">
            <td>{{ item.id }}</td>
            <td>{{ item.reason }}</td>
            <td>{{ item.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
