import { useQuery } from "@tanstack/react-query";
import { Binary, Braces, CaseLower, CheckCheck, ListChecks, Regex, Scale } from "lucide-react";
import { api } from "../api";
import { EmptyState, ErrorNotice, PageHead } from "../ui";
import { SuiteModuleTabs } from "./SuiteModuleTabs";

const icons = {
  exact_match: CheckCheck,
  normalized_exact_match: CaseLower,
  multiple_choice_accuracy: ListChecks,
  token_f1: Binary,
  contains_all: CheckCheck,
  regex_match: Regex,
  json_schema_valid: Braces,
};

export function EvaluationScorers() {
  const scorers = useQuery({ queryKey: ["evaluation-scorers"], queryFn: api.evaluationScorers });
  return (
    <div className="page-stack scorers-page">
      <SuiteModuleTabs />
      <PageHead eyebrow="DETERMINISTIC SCORING / 评分器" title="每一分都能被相同代码重复计算" description="评分器来自只读代码注册表。用户不能上传 Python 或 JavaScript，也不会产生隐藏的 LLM Judge 调用。" />
      {scorers.error && <ErrorNotice error={scorers.error} />}
      <div className="scorer-grid stagger-grid">
        {scorers.data?.map((scorer) => {
          const Icon = icons[scorer.scorer_id as keyof typeof icons] ?? Scale;
          return <article className="scorer-card" key={scorer.scorer_id}><div className="scorer-icon"><Icon size={21} /></div><header><span>{scorer.scorer_id}</span><h2>{scorer.label}</h2></header><p>{scorer.description}</p><footer><code>v{scorer.version}</code><span><i />{scorer.execution}</span></footer></article>;
        })}
      </div>
      {!scorers.data?.length && !scorers.isLoading && <div className="panel"><EmptyState icon={Scale} title="评分器目录不可用" body="确认后端评测模块和迁移已启用。" /></div>}
      <section className="panel scorer-boundary"><header><Scale size={18} /><h2>统一失败语义</h2></header><div><span>PASS</span><p>确定性规则得到完整匹配或合法结构。</p></div><div><span>FAIL</span><p>证据存在且明确与参考不符。</p></div><div><span>UNKNOWN</span><p>缺少证据、输出无法解析或评分器无法安全执行；绝不编造 0 分。</p></div></section>
    </div>
  );
}
