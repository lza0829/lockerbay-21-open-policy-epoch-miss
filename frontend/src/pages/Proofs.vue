<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const epochs = ref([])
const generations = ref([])
const live = ref(null)
const currentHours = ref(null)
const detail = ref(null)
const detailErr = ref('')

const unverified = computed(() => epochs.value.filter((e) => !e.verified))
const drift = computed(() => epochs.value.filter((e) => e.epoch_drift))

async function load() {
  proofs.value = await api.proofs()
  const body = await api.epochs()
  epochs.value = body.rows
  generations.value = body.generations
  const gen = await api.generations()
  live.value = gen.live
  const dl = await api.deadlines()
  currentHours.value = dl.current_overdue_hours
}
async function inspect(id) {
  detailErr.value = ''
  try {
    detail.value = await api.policyEpoch(id)
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
    <p class="sub">
      占用核对与 deadline / 柜规世代核对汇总；截止时刻与 policy_epoch 均取开账时物化值，不在页面重算，
      也不拿现行世代回放旧占用。
    </p>

    <div class="stats">
      <div class="stat"><b>{{ epochs.length }}</b><span>核对占用</span></div>
      <div class="stat"><b>{{ unverified.length }}</b><span>世代未对齐</span></div>
      <div class="stat"><b>{{ live?.policy_epoch ?? '—' }}</b><span>现行世代</span></div>
      <div class="stat"><b>{{ currentHours ?? '—' }}</b><span>现行 overdue_hours</span></div>
    </div>

    <div class="panel" style="overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">世代核证（只读 <code>GET /api/policy-epochs</code>）</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>开启</th><th>截止</th><th>所用小时</th><th>开账世代</th><th>该世代小时</th><th>现行世代</th><th>判定</th><th></th></tr></thead>
        <tbody>
          <tr v-for="e in epochs" :key="e.occupancy_id">
            <td><code>{{ e.occupancy_id }}</code></td>
            <td>{{ e.cell_id }}</td>
            <td>{{ e.opened_at }}</td>
            <td><code>{{ e.reclaim_deadline_at || '缺列' }}</code></td>
            <td>{{ e.overdue_hours_used ?? '—' }}</td>
            <td><code>{{ e.policy_epoch ?? '缺' }}</code></td>
            <td>{{ e.epoch_overdue_hours ?? '—' }}</td>
            <td><code>{{ e.live_policy_epoch ?? '—' }}</code></td>
            <td>{{ e.verified ? '对齐' : '不一致' }}<span v-if="e.epoch_drift"> · 旧世代</span></td>
            <td><button @click="inspect(e.occupancy_id)">单笔</button></td>
          </tr>
        </tbody>
      </table>
      <p class="sub" style="margin:.6rem 0 0">
        {{ unverified.length }} 笔世代未对齐；{{ drift.length }} 笔按旧世代开账（现行世代变了但占用 epoch 未改写）。
      </p>
    </div>

    <div v-if="detail" class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">单笔核证 · {{ detail.occupancy_id }}</h2>
      <pre>{{ detail }}</pre>
    </div>
    <p v-if="detailErr" class="sub">{{ detailErr }}</p>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">柜规世代登记（append-only）</h2>
      <table>
        <thead><tr><th>世代</th><th>overdue_hours</th><th>登记于</th></tr></thead>
        <tbody>
          <tr v-for="g in generations" :key="g.policy_epoch">
            <td><code>{{ g.policy_epoch }}</code></td>
            <td>{{ g.overdue_hours }}</td>
            <td>{{ g.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">deadline 核对（只读）</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>截止</th><th>所用小时</th><th>世代</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.occupancy_id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td><code>{{ p.reclaim_deadline_at || '缺列' }}</code></td>
            <td>{{ p.overdue_hours_used ?? '—' }}</td>
            <td><code>{{ p.policy_epoch ?? '缺' }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>

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
