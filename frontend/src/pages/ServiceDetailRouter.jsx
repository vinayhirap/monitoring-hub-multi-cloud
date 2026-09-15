// monitoring-hub/frontend/src/pages/ServiceDetailRouter.jsx
//
// Single entry point for /accounts/:id/:service. Decides between the
// two detail-page components based on whether a bespoke, chart-heavy
// page exists for this service key (ServiceDetail.hasCoreDetailPage) —
// keeps ServiceDetail.jsx itself untouched (still exactly the 7-service
// page it always was) while making every OTHER service — every
// AWS-extended service, and every GCP/Azure service — open a real
// in-app page instead of either a console redirect or an unreachable
// dead link. See ServiceList.jsx's ServiceCard: every tile there now
// routes here regardless of which kind of service it is.
import { useParams } from "react-router-dom";
import ServiceDetail, { hasCoreDetailPage } from "./ServiceDetail";
import GenericServiceDetail from "./GenericServiceDetail";

export default function ServiceDetailRouter() {
  const { id, service } = useParams();
  if (hasCoreDetailPage(service)) return <ServiceDetail />;
  return <GenericServiceDetail accountId={id} service={service} />;
}
