// src/pages/EmailCenter.jsx
//
// Email & Reports: one place to see SMTP state, manage recipient lists and report schedules, and read the delivery log.
// Built on /api/email-center/* (app/api/email_center.py). The route is /email-center/:tab and only exists where
// EMAIL_CENTER_ENABLED=true (App.jsx ROUTE_ACCESS + the nav entry). Each tab and write button has its own permission:
// .view = status, schedules and rules (no addresses); .manage = change schedules and rules, run now; .recipients = the
// recipient lists; .log = the delivery log; .test = the test email. See emailCenterTabs in utils/emailCenterView.js.
import { useCallback, useEffect, useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { PageHeader, SegmentedControl } from "../components/ui";
import { getOptions, errMsg } from "../api/emailCenter";
import StatusTab from "./emailcenter/StatusTab";
import RecipientsTab from "./emailcenter/RecipientsTab";
import SchedulesTab from "./emailcenter/SchedulesTab";
import RulesTab from "./emailcenter/RulesTab";
import DeliveryLogTab from "./emailcenter/DeliveryLogTab";
import { emailCenterTabs } from "../utils/emailCenterView";
import "./EmailCenter.css";

export default function EmailCenter() {
  const { tab } = useParams();
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("emailcenter.manage");
  const tabs = emailCenterTabs(hasPermission);
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
        subtitle="Whether email is working, who receives what, scheduled reports, alert and incident email rules, and a record of every email CloudOps sent." />
      <div className="ec-tabs">
        <SegmentedControl label="Email & Reports sections" options={tabs} value={tab} onChange={k => navigate(`/email-center/${k}`)} />
      </div>
      {optionsError && <div className="ec-error" role="alert">Could not load the pickers: {optionsError}</div>}
      {tab === "status" && <StatusTab />}
      {tab === "recipients" && <RecipientsTab options={options} onChanged={loadOptions} />}
      {tab === "schedules" && <SchedulesTab options={options} canManage={canManage} />}
      {tab === "rules" && <RulesTab options={options} canManage={canManage} />}
      {tab === "log" && <DeliveryLogTab />}
    </div>
  );
}
