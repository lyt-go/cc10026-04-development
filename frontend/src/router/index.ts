import { createRouter, createWebHistory } from 'vue-router'

import FilterTypeReportView from '@/views/FilterTypeReportView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/filter-type-reports', name: 'filter-type-reports', component: FilterTypeReportView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
