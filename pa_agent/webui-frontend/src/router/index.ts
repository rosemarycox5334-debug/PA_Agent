/**
 * Lightweight router. The original SPA uses tab-style switching rather
 * than deep URLs, so we register a single `/:tab` route and let the
 * App.vue shell render the matching view component. Each tab has a
 * stable path so the back/forward buttons and bookmarks still work.
 */
import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router';

const TerminalView = () => import('@/views/TerminalView.vue');
const DecisionView = () => import('@/views/DecisionView.vue');
const RecordsView = () => import('@/views/RecordsView.vue');
const SettingsView = () => import('@/views/SettingsView.vue');

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/terminal' },
  { path: '/terminal', name: 'terminal', component: TerminalView, meta: { tab: 'terminal' } },
  { path: '/decision', name: 'decision', component: DecisionView, meta: { tab: 'decision' } },
  { path: '/records', name: 'records', component: RecordsView, meta: { tab: 'records' } },
  { path: '/settings', name: 'settings', component: SettingsView, meta: { tab: 'settings' } },
  { path: '/:pathMatch(.*)*', redirect: '/terminal' },
];

export const router = createRouter({
  // Hash history keeps the SPA working whether it is served from
  // /webui/ (production) or / (dev preview) without any server-side
  // fallback configuration.
  history: createWebHashHistory(),
  routes,
});

export default router;
