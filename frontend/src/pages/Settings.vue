<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const hours = ref(48)
const msg = ref('')
const live = ref(null)
const generations = ref([])

async function load() {
  hours.value = (await api.settings()).overdue_hours
  const gen = await api.generations()
  live.value = gen.live
  generations.value = gen.generations
}
onMounted(load)
async function save() {
  const r = await api.setOverdue(Number(hours.value))
  hours.value = r.overdue_hours
  msg.value = `已保存 · 新世代 ${r.policy_epoch ?? '—'}`
  await load()
}
</script>
<template>
  <div data-plant="21-open-policy-epoch-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>柜规</h1>
    <p class="sub">
      超时回收小时数：开占用时把绝对截止时刻与柜规世代（policy_epoch）同笔物化；
      改这里只影响之后新开的占用，已落地 deadline 与旧占用的 epoch 不改写。
    </p>
    <div class="panel" style="max-width:480px; display:flex; gap:.75rem; align-items:center">
      <label>overdue_hours
        <input v-model.number="hours" type="number" min="1" style="margin-left:.5rem; width:5rem; background:#111827; color:#e8eef6; border:1px solid #334155; border-radius:4px; padding:.3rem" />
      </label>
      <button @click="save">保存</button>
      <span class="sub">{{ msg }}</span>
    </div>
    <p class="sub" style="margin-top:.75rem">
      现行柜规世代：<code>{{ live?.policy_epoch ?? '未登记' }}</code>
      · 该世代 overdue_hours：<code>{{ live?.overdue_hours ?? '—' }}</code>
      <template v-if="live && !live.registered">（已改柜规、尚未开账，下一次开账时登记新世代）</template>
    </p>

    <div class="panel" style="margin-top:1rem; max-width:560px">
      <h2 style="margin:0 0 .5rem;font-size:1rem">柜规世代登记（append-only，不可改不可删）</h2>
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
  </div>
</template>
