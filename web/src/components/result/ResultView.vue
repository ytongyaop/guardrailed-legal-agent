<script setup lang="ts">
/**
 * 核对结果（规格 005 三层信息架构）——**只渲染 DTO，不做任何业务计算**。
 *
 * | 层 | 内容 | 开关 |
 * | --- | --- | --- |
 * | 第一层 结论 | 结论句 + 第一层明细 + 提醒；条目（条号常驻、原文折叠）；限制提示；必备条款独立计数；处理建议正文 + 导出 Word | 常驻 |
 * | 第二层 依据 | 依据事实（折叠）、类案参照（折叠） | 常驻 |
 * | 第三层 过程 | 引用数、耗时、覆盖明细、来源/等级、补问、降级、运行详情 | `detail.process !== null` 才挂载 |
 *
 * 三条硬规则：
 * 1. 分组只用服务端给的 `impact`，`filter` 保持服务端顺序（不重排、不重数）；
 * 2. 过程开关关闭时**不渲染**第三层——`v-if` 在组件层面，不是 `v-show` / CSS 隐藏；
 * 3. 免责声明每个结果页只出现一次，固定在最后一行。
 */
import { computed } from 'vue'

import type { CaseItemView, FindingView, TaskDetail } from '@/api/types'
import { safeSourceUrl } from '@/utils/format'
import FindingCard from './FindingCard.vue'
import FoldBox from './FoldBox.vue'
import MissingTermsBlock from './MissingTermsBlock.vue'
import ProcessLayer from './ProcessLayer.vue'

const props = defineProps<{ detail: TaskDetail; busy?: boolean }>()
const emit = defineEmits<{ 'download-report': [] }>()

/** 影响度分组顺序（与 `domain/grading.py` 的 IMPACT_RANK 一致）；组内顺序沿用服务端。 */
const IMPACT_GROUPS: readonly { impact: string; title: string }[] = [
  { impact: '高', title: '高影响' },
  { impact: '中', title: '中影响' },
  { impact: '低', title: '低影响' },
]

const impactGroups = computed(() =>
  IMPACT_GROUPS.map((group) => ({
    ...group,
    items: props.detail.findings.filter((row: FindingView) => row.impact === group.impact),
  })).filter((group) => group.items.length > 0),
)

const limits = computed(() => props.detail.limits)
const hasLimits = computed(
  () =>
    limits.value.unmet_lines.length > 0 ||
    limits.value.field_missing_lines.length > 0 ||
    limits.value.premise_missing_lines.length > 0 ||
    limits.value.statutory_na_line !== null,
)

const verdictTone = computed(() => {
  if (props.detail.verdict.status === 'risk') return 'danger'
  if (props.detail.verdict.status === 'clean') return 'success'
  return 'plain'
})

function caseLink(item: CaseItemView): string | null {
  return safeSourceUrl(item.source_url)
}
</script>

<template>
  <div class="result" data-testid="result">
    <!-- 拒答是成功结果的一种：不渲染任何结论层，只说明原因与下一步 -->
    <el-alert
      v-if="detail.refusal"
      class="page-card"
      type="error"
      :closable="false"
      show-icon
      :title="detail.refusal.message"
      data-testid="refusal"
    >
      <p>{{ detail.refusal.guidance }}</p>
    </el-alert>

    <template v-else>
      <!-- ---- 第一层 结论 ---- -->
      <section class="result-verdict" :data-status="detail.verdict.status">
        <p class="page-caption" data-testid="task-meta">
          {{ detail.task.contract_name }} · {{ detail.task.created_at ?? '时间未记录' }} ·
          {{ detail.task.shared ? '已存入企业档案' : '仅自己可见' }}
        </p>
        <h2 :class="['verdict-headline', `verdict-${verdictTone}`]" data-testid="verdict-headline">
          {{ detail.verdict.headline }}
        </h2>
        <p v-if="detail.verdict.conclusion_details.length" class="page-caption" data-testid="verdict-details">
          {{ detail.verdict.conclusion_details.join(' · ') }}
        </p>
        <p v-if="detail.verdict.caveat" class="verdict-caveat" data-testid="verdict-caveat">
          {{ detail.verdict.caveat }}
        </p>
      </section>

      <el-card class="page-card" shadow="never" data-testid="findings">
        <h3 class="section-title">核对结论</h3>
        <p v-if="detail.findings.length === 0" class="page-caption">本次没有可列出的结论条目。</p>
        <template v-for="group in impactGroups" :key="group.impact">
          <h4 class="impact-title" :data-testid="`impact-${group.impact}`">{{ group.title }}</h4>
          <finding-card v-for="(row, index) in group.items" :key="row.item_id || `${group.impact}-${index}`" :finding="row" />
        </template>
      </el-card>

      <!-- 第一层：合同未约定 / 需补充事实 / 法定兜底（结论边界，属第一层） -->
      <el-card v-if="hasLimits" class="page-card" shadow="never" data-testid="limits">
        <template v-if="limits.unmet_lines.length">
          <h4 class="impact-title">合同未约定</h4>
          <p v-for="line in limits.unmet_lines" :key="line" class="page-caption">{{ line }}</p>
        </template>
        <template v-if="limits.field_missing_lines.length || limits.premise_missing_lines.length">
          <h4 class="impact-title">需补充事实</h4>
          <p v-for="line in limits.field_missing_lines" :key="line" class="page-caption">{{ line }}</p>
          <fold-box
            v-if="limits.premise_missing_lines.length"
            :title="`另有 ${limits.premise_count} 条规则因前提未确定而未判定`"
            testid="premise-fold"
          >
            <p v-for="line in limits.premise_missing_lines" :key="line" class="page-caption">{{ line }}</p>
          </fold-box>
        </template>
        <p v-if="limits.statutory_na_line" class="page-caption">{{ limits.statutory_na_line }}</p>
      </el-card>

      <!-- 第一层：必备条款完整性（独立计数，不并入冲突/待确认） -->
      <missing-terms-block :terms="detail.missing_terms" />

      <!-- 第一层收尾：处理建议正文（不折叠）＋导出 Word -->
      <el-card class="page-card" shadow="never" data-testid="suggestions">
        <h3 class="section-title">处理建议（非法律意见）</h3>
        <ul v-if="detail.suggestions.length" class="suggestion-list">
          <li v-for="(row, index) in detail.suggestions" :key="index" class="suggestion">
            <p class="suggestion-text">{{ row.text }}</p>
            <p class="page-caption">
              {{ row.citation_text }}<template v-if="row.display_disclaimer"> · {{ row.display_disclaimer }}</template>
            </p>
          </li>
        </ul>
        <p v-else-if="detail.suggestion_empty_text" class="page-caption" data-testid="suggestions-empty">
          {{ detail.suggestion_empty_text }}
        </p>
        <div class="page-actions">
          <el-button :loading="busy" @click="emit('download-report')">导出 Word 报告</el-button>
        </div>
      </el-card>

      <!-- ---- 第二层 依据（默认折叠，条号在折叠器外） ---- -->
      <el-card class="page-card" shadow="never" data-testid="evidence-layer">
        <h3 class="section-title">依据</h3>
        <fold-box v-if="detail.facts.length" :title="`依据事实（${detail.facts.length} 项）`" testid="facts-fold">
          <dl class="facts">
            <template v-for="row in detail.facts" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd>{{ row.value }}</dd>
            </template>
          </dl>
        </fold-box>
        <fold-box title="类案参照" testid="cases-fold">
          <p class="page-caption">{{ detail.cases.note }} {{ detail.cases.summary }}</p>
          <div v-for="item in detail.cases.items" :key="item.title" class="case-item">
            <p class="case-title"><strong>{{ item.title }}</strong></p>
            <p class="page-caption">{{ item.holding }}</p>
            <p class="page-caption">
              {{ item.source }} · {{ item.dispute_type }}
              <a v-if="caseLink(item)" :href="caseLink(item) ?? undefined" target="_blank" rel="noopener noreferrer">
                查看来源
              </a>
            </p>
          </div>
        </fold-box>
      </el-card>

      <!-- ---- 第三层 过程：条件挂载，开关关闭时这里什么都不存在 ---- -->
      <process-layer v-if="detail.process" :process="detail.process" />

      <!-- 就本合同继续提问（历史只读详情不传这个插槽） -->
      <slot name="qa" />

      <p class="disclaimer" data-testid="disclaimer">{{ detail.disclaimer }}</p>
    </template>
  </div>
</template>
