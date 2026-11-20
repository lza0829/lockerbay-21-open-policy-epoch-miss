<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const deadlines = ref([])
const currentHours = ref(null)
const detail = ref(null)
const detailErr = ref('')

async function load() {
  proofs.value = await api.proofs()
  const body = await api.deadlines()
  deadlines.value = body.rows
  currentHours.value = body.current_overdue_hours
}
async function inspect(id) {
  detailErr.value = ''
  try {
    detail.value = await api.deadline(id)
  } catch (e) {
    detail.value = null
    detailErr.value = String(e.message || e)
  }
}
onMounted(load)
</script>
<template>
  <div data-plant="21-open-policy-epoch-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">占用核对与 deadline 核对汇总；截止时刻取开账时物化值，不在页面重算。</p>

    <div class="panel" style="overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">deadline 核对（只读）</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>截止</th><th>所用小时</th><th></th></tr></thead>
        <tbody>
          <tr v-for="p in deadlines" :key="p.occupancy_id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td><code>{{ p.reclaim_deadline_at || '缺列' }}</code></td>
            <td>{{ p.overdue_hours_used ?? '—' }}</td>
            <td><button @click="inspect(p.occupancy_id)">单笔</button></td>
          </tr>
        </tbody>
      </table>
      <p class="sub" style="margin:.6rem 0 0">现行 overdue_hours：{{ currentHours ?? '—' }}</p>
    </div>

    <div v-if="detail" class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">单笔核对 · {{ detail.occupancy_id }}</h2>
      <pre>{{ detail }}</pre>
    </div>
    <p v-if="detailErr" class="sub">{{ detailErr }}</p>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用核对</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>事件</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td><code>{{ p.event_ids_json }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
