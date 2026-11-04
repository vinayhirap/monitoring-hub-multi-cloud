// src/pages/EmailCenter.jsx
//
// Email & Reports: one place to see SMTP state, manage recipient lists and report schedules, and read the delivery log.
// Built on /api/email-center/* (app/api/email_center.py). The route is /email-center/:tab and only exists where
// EMAIL_CENTER_ENABLED=true (App.jsx ROUTE_ACCESS + the nav entry). emailcenter.view sees SMTP state and the schedules
// (without addresses); emailcenter.manage also gets recipients, the delivery log and every write button.
import { useCallback, useEffect, useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { PageHeader, SegmentedControl } from "../components/ui";
import { getOptions, errMsg } from "../api/emailCenter";
import SmtpTab from "./emailcenter/SmtpTab";
import RecipientsTab from "./emailcenter/RecipientsTab";
import SchedulesTab from "./emailcenter/SchedulesTab";
import RulesTab from "./emailcenter/RulesTab";
import DeliveryLogTab from "./emailcenter/DeliveryLogTab";
import "./EmailCenter.css";

const ALL_TABS = [
  { key: "smtp", label: "SMTP", manage: false },
  { key: "recipients", label: "Recipients", manage: true },
  { key: "schedules", label: "Report schedules", manage: false },
  { key: "rules", label: "Alert & incident rules", manage: false },
  { key: "log", label: "Delivery log", manage: true },
];

export default function EmailCenter() {
  const { tab } = useParams();
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("emailcenter.manage");
  const tabs = ALL_TABS.filter(t => canManage || !t.manage);
  const [options, setOptions] = useState(null);
  const [optionsError, setOptionsError] = useState("");

  const loadOptions = useCallback(() => {
    getOptions().then(o => { setOptions(o); setOptionsError(""); }).catch(e => setOptionsError(errMsg(e)));
  }, []);
  useEffect(() => { loadOptions(); }, [loadOptions]);

  if (!tabs.some(t => t.key === tab)) return <Navigate to={`/email-center/${tabs[0].key}`} replace />;

  return (
    <div className="ec-page">
      <PageHeader
        title="Email & Reports"
        subtitle="SMTP status, who receives what, scheduled weekly, monthly, quarterly and yearly reports, alert and incident email rules, and a record of every email CloudOps sent." />
      <div className="ec-tabs">
        <SegmentedControl label="Email & Reports sections" options={tabs} value={tab} onChange={k => navigate(`/email-center/${k}`)} />
      </div>
      {optionsError && <div className="ec-error" role="alert">Could not load the pickers: {optionsError}</div>}
      {tab === "smtp" && <SmtpTab canManage={canManage} />}
      {tab === "recipients" && <RecipientsTab options={options} onChanged={loadOptions} />}
      {tab === "schedules" && <SchedulesTab options={options} canManage={canManage} />}
      {tab === "rules" && <RulesTab options={options} canManage={canManage} />}
      {tab === "log" && <DeliveryLogTab />}
    </div>
  );
}
