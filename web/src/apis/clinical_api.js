import { apiGet, apiPost } from './base'

/**
 * ClinEvidence 患者域 API:患者、就诊与导入批次。
 * 所有读取都走服务端可见性;前端不拼接患者作用域参数。
 */
export const clinicalApi = {
  listPatients: () => apiGet('/api/clinical/patients'),

  createPatient: ({ displayCode = null } = {}) =>
    apiPost('/api/clinical/patients', displayCode ? { display_code: displayCode } : {}),

  listEncounters: (patientId) => apiGet(`/api/clinical/patients/${patientId}/encounters`),

  getImportBatch: (batchId) => apiGet(`/api/clinical/import-batches/${batchId}`),

  confirmBatchAssignment: (batchId, method = 'manual') =>
    apiPost(`/api/clinical/import-batches/${batchId}/confirm-identity`, { method })
}
