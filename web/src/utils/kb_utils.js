import { Database, DatabaseZap } from '@lucide/vue'

export const getKbTypeLabel = (type) =>
  String(type || '').toLowerCase() === 'milvus' ? 'ClinEvidence' : `${type}（不支持）`

export const getKbTypeIcon = (type) => (type === 'milvus' ? DatabaseZap : Database)
export const getKbTypeColor = () => 'blue'

export const isReadOnlyDatabase = (database) => {
  const type = typeof database === 'string' ? database : database?.kb_type || 'milvus'
  return type.toLowerCase() !== 'milvus'
}

export const kbUtils = { getKbTypeLabel, getKbTypeIcon, getKbTypeColor, isReadOnlyDatabase }
