export type AppRoute = {
  feature: 'duplicates' | 'ingestion';
  page: string;
  runId?: string;
};
const duplicatePages = ['runs', 'tasks', 'sources', 'operations'];
const ingestionPages = ['workbench', 'mineru', 'sources', 'history', 'settings'];
export function parseRoute(hash: string): AppRoute {
  const [feature, page, runId, extra] = hash.replace(/^#\/?/, '').split('/');
  if (feature === 'ingestion' && ingestionPages.includes(page) && !runId) {
    return {feature, page};
  }
  if (feature === 'duplicates' && duplicatePages.includes(page) && !extra) {
    if (!runId) return {feature, page};
    if (page === 'runs' && /^[a-zA-Z0-9-]+$/.test(runId)) return {feature, page, runId};
  }
  return {feature: 'duplicates', page: 'runs'};
}
export function routeHash(route: AppRoute): string {
  return `#/${route.feature}/${route.page}` + (route.runId ? `/${route.runId}` : '');
}
