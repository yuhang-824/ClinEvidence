import { apiDelete, apiGet, apiPost, apiRequest } from './base'

/**
 * ClinEvidence 患者域 API:患者、就诊与导入批次。
 * 所有读取都走服务端可见性;前端不拼接患者作用域参数。
 */
export const clinicalApi = {
  listPatients: () => apiGet('/api/clinical/patients'),

  createPatient: ({ displayCode = null, category } = {}) =>
    apiPost('/api/clinical/patients', {
      ...(displayCode ? { display_code: displayCode } : {}),
      category
    }),

  updatePatient: (patientId, payload) =>
    apiRequest(`/api/clinical/patients/${patientId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }),

  listEncounters: (patientId) => apiGet(`/api/clinical/patients/${patientId}/encounters`),

  getImportBatch: (batchId) => apiGet(`/api/clinical/import-batches/${batchId}`),

  confirmBatchAssignment: (batchId, method = 'manual') =>
    apiPost(`/api/clinical/import-batches/${batchId}/confirm-identity`, { method })
}

Object.assign(clinicalApi, {
  getPatientLibrary: (patientId) => apiGet(`/api/clinical/patients/${patientId}/library`),

  getRevisionChunks: (revisionId) => apiGet(`/api/clinical/revisions/${revisionId}/chunks`),

  uploadRecordTmp: (threadId, file) => {
    const form = new FormData()
    form.append('file', file)
    return apiPost(`/api/clinical/threads/${threadId}/records/tmp`, form)
  },

  confirmRecordUpload: (threadId, payload) =>
    apiPost(`/api/clinical/threads/${threadId}/records/confirm`, payload),

  approveRevision: (revisionId) => apiPost(`/api/clinical/revisions/${revisionId}/approve`, {}),

  buildRevisionChunks: (revisionId) => apiPost(`/api/clinical/revisions/${revisionId}/chunks`, {}),

  indexRevision: (revisionId) => apiPost(`/api/clinical/revisions/${revisionId}/index`, {}),

  publishBatch: (batchId) => apiPost(`/api/clinical/import-batches/${batchId}/publish`, {}),

  finalizeBatch: (batchId) =>
    apiPost(
      `/api/clinical/import-batches/${batchId}/finalize`,
      {},
      { timeout: 300000 }
    )
})

Object.assign(clinicalApi, {
  deletePatient: (patientId) => apiDelete(`/api/clinical/patients/${patientId}`)
})

Object.assign(clinicalApi, {
  uploadPatientTmp: (patientId, file) => {
    const form = new FormData()
    form.append('file', file)
    return apiPost(`/api/clinical/patients/${patientId}/records/tmp`, form)
  },

  confirmPatientUpload: (patientId, payload) =>
    apiPost(`/api/clinical/patients/${patientId}/records/confirm`, payload)
})
