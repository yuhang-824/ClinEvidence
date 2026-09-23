import { apiGet, apiPost } from './base'

const root = '/api/clinical/evaluation'

export const clinicalRagasApi = {
  options: () => apiGet(`${root}/options`),
  listDatasets: () => apiGet(`${root}/datasets`),
  getDataset: (id) => apiGet(`${root}/datasets/${id}`),
  listRuns: () => apiGet(`${root}/runs`),
  getRun: (id) => apiGet(`${root}/runs/${id}`),
  startRun: (payload) => apiPost(`${root}/runs`, payload),
  uploadDataset: ({ name, samples, metadata, scopeMap }) => {
    const form = new FormData()
    form.append('name', name)
    form.append('samples', samples)
    if (metadata) form.append('metadata', metadata)
    if (scopeMap) form.append('scope_map', scopeMap)
    return apiPost(`${root}/datasets`, form)
  }
}
