import { BarChart3, DatabaseZap, FlaskConical, Scale } from "lucide-react";
import { NavLink } from "react-router-dom";

const tabs = [
  { to: "/suites", label: "探测套件", icon: FlaskConical, end: true },
  { to: "/suites/datasets", label: "评测数据集", icon: DatabaseZap, end: false },
  { to: "/suites/scorers", label: "评分器", icon: Scale, end: true },
  { to: "/suites/evaluation-runs", label: "评测记录", icon: BarChart3, end: false },
] as const;

export function SuiteModuleTabs() {
  return (
    <nav className="suite-module-tabs" aria-label="探测套件管理">
      {tabs.map(({ to, label, icon: Icon, end }) => (
        <NavLink key={to} to={to} end={end}>
          <Icon size={15} />
          <span>{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
