<script setup lang="ts">
/**
 * 趋势图（规格 006 §1：ECharts **按需引入**，只服务这一张图）。
 *
 * - 只 `use` 柱状图与用到的组件/渲染器：不 `import 'echarts'` 全量包；
 * - 组件本身由 `HistoryView` 异步加载，ECharts 因此落在独立 chunk，不拖慢首屏；
 * - 画什么完全来自服务端 `TrendView.points`（最近 12 次、时间正序），前端不重算口径。
 */
import { BarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { TrendPoint } from '@/api/types'

echarts.use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ points: TrendPoint[] }>()

const host = ref<HTMLElement | null>(null)
let chart: ReturnType<typeof echarts.init> | null = null

function buildOption(): Record<string, unknown> {
  const labels = props.points.map((point) => point.label)
  return {
    grid: { left: 8, right: 8, top: 36, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, data: ['风险条目', '处理建议'] },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { interval: 0, rotate: -40, fontSize: 11 },
      axisTick: { alignWithLabel: true },
    },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#E2E8F0' } } },
    series: [
      {
        name: '风险条目',
        type: 'bar',
        barMaxWidth: 26,
        itemStyle: { color: '#1E40AF' },
        data: props.points.map((point) => point.risk_item_count),
      },
      {
        name: '处理建议',
        type: 'bar',
        barMaxWidth: 26,
        itemStyle: { color: '#0F766E' },
        data: props.points.map((point) => point.suggestion_count),
      },
    ],
  }
}

function draw(): void {
  if (host.value === null) return
  try {
    chart ??= echarts.init(host.value)
    chart.setOption(buildOption(), true)
  } catch {
    // jsdom 等无 canvas 环境：图不可画，但页面其余部分（指标与文案）照常可用
    chart = null
  }
}

onMounted(draw)
watch(() => props.points, draw, { deep: true })

onBeforeUnmount(() => {
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="host" class="trend-chart" data-testid="trend-chart"></div>
</template>
