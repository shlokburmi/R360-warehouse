/** Named AppNotification, not Notification — that name is already taken by
 * the browser's own Web Notifications API global. */
export type AppNotification = {
  id: string
  title: string
  body: string
  payload: Record<string, unknown> | null
  created_at: string
  gate_entry_id: string | null
  exception_id: string | null
}

export type Person = {
  visitor_id: string
  full_name: string
  mobile: string
  visitor_role: 'driver' | 'laborer' | 'supervisor'
  has_id_photo: boolean
  is_returning_visitor: boolean
}

export type GateEntry = {
  id: string
  entry_code: string
  status: string
  vehicle_number: string
  vendor_id: string
  vendor_name: string | null
  vendor_is_active: boolean
  purchase_order_id: string | null
  po_number: string | null
  po_reference_note: string | null
  transporter_name: string | null
  requested_by: string
  requested_by_name: string | null
  requested_at: string | null
  decided_by: string | null
  decided_by_name: string | null
  decided_at: string | null
  decision_note: string | null
  sla_breached: boolean
  escalated_at: string | null
  time_in: string | null
  time_out: string | null
  declared_box_count: number | null
  issued_box_sticker_count: number
  scanned_box_count: number
  persons: Person[]
  created_at: string
}

export type VisitorLookup = {
  found: boolean
  visitor_id: string | null
  full_name: string | null
  photo_required: boolean
  reason: string
  last_seen_at: string | null
  is_blocked: boolean
  blocked_reason: string | null
}

export type Box = {
  id: string
  box_number: number
  status: string
  sticker_code: string | null
  expected_units: number
  scanned_units: number
  quarantined_units: number
  sku: string | null
  description: string | null
  damage_level: string | null
  damage_note: string | null
  verified_at: string | null
  completed_at: string | null
}

export type Sticker = {
  id: string
  code: string
  sticker_type: 'box' | 'unit'
  status: string
  sequence_no: number
  expected_units: number | null
  box_id: string | null
  box_number: number | null
  sku: string | null
  description: string | null
}

export type StickerSheet = {
  id: string
  gate_entry_id: string
  sticker_type: 'box' | 'unit'
  quantity: number
  generated_at: string
  generated_by_name: string | null
  stickers: Sticker[]
}

export type ScanResult = {
  client_event_id: string
  accepted: boolean
  duplicate: boolean
  reject_reason: string | null
  message: string
  box_number: number | null
  box_id: string | null
  scanned_units: number | null
  expected_units: number | null
}

export type Progress = {
  total: number
  scanned: number
  remaining: number
  complete: boolean
  message: string
}

export type WarehouseException = {
  id: string
  exception_code: string
  exception_type: string
  status: string
  title: string
  details: Record<string, unknown>
  gate_entry_id: string | null
  entry_code: string | null
  box_id: string | null
  box_number: number | null
  vendor_name: string | null
  po_number: string | null
  reported_by_name: string | null
  reported_at: string
  escalated_at: string | null
  resolution: string | null
  resolution_note: string | null
  resolved_by_name: string | null
  resolved_at: string | null
}

export type Vendor = { id: string; code: string; name: string }

export type PurchaseOrder = {
  id: string
  po_number: string
  status: string
  expected_on: string | null
  vendor_name: string
  vendor_id: string
  expected_units: number
  line_count: number
}

export type PurchaseOrderLine = {
  id: string
  line_no: number
  sku: string
  description: string | null
  expected_units: number
  units_per_box: number
  received_units: number
  rejected_units: number
  expected_boxes: number
}


export type ReconcileLine = {
  purchase_order_line_id: string
  sku: string
  description: string
  expected_units: number
  warehouse_count: number
  inbound_count: number | null
  matched: boolean | null
}

export type Reconciliation = {
  gate_entry_id: string
  lines: ReconcileLine[]
  all_matched: boolean
  message: string
  exception_code?: string | null
}

// --- Phase 2: putaway ------------------------------------------------------

export type PutawayTask = {
  box_id: string
  gate_entry_id: string
  box_number: number
  box_status: string
  stock_remaining: number
  quarantine_remaining: number
  entry_code: string
  vehicle_number: string
  vendor_name: string
  po_number: string | null
  sku: string | null
  description: string | null
}

export type Location = {
  id: string
  code: string
  zone: string
  description: string | null
  is_quarantine: boolean
}

export type BoxPutawayStatus = {
  box_id: string
  box_number: number
  box_status: string
  scanned_units: number
  quarantined_units: number
  stock_units: number
  stock_placed: number
  quarantine_placed: number
  stock_remaining: number
  quarantine_remaining: number
  sku: string | null
  description: string | null
  entry_code: string
  entry_status: string
}

export type PutawayResult = {
  box: BoxPutawayStatus
  location: Location
  units_placed: number
  complete: boolean
  message: string
}

export type StockRow = {
  location_code: string
  zone: string
  is_quarantine: boolean
  sku: string
  description: string
  units: number
  last_movement: string | null
}

// --- Phase 3: invoice matching, packing, batches ---------------------------

export type BadgeHolder = {
  profile_id: string
  full_name: string
  role: string
  employee_code: string | null
}

export type Invoice = {
  invoice_id: string
  invoice_number: string
  /** Order No off the challan header, e.g. CP002458380_0001. Null until read. */
  order_no: string | null
  sku: string
  units: number
  customer_name: string | null
  description: string | null
  is_open: boolean
  stage: 'open' | 'verified' | 'packed' | 'batched' | 'out_scanned' | 'closed'
  verified_by: string | null
  verified_by_name: string | null
  verified_at: string | null
  packed_by: string | null
  packed_by_name: string | null
  packed_at: string | null
  batch_id: string | null
  batch_code: string | null
  batch_status: string | null
  out_scanned_at: string | null
  suggested_locations: { location_code: string; units: number }[]
}

export type OrderNoResult = {
  invoice: Invoice
  /** False when the read missed — the attempt is logged, the invoice untouched. */
  recorded: boolean
  message: string
}

export type AttributionResult = {
  invoice: Invoice
  who: BadgeHolder
  message: string
}

export type Carton = {
  invoice_id: string
  invoice_number: string
  sku: string
  units: number
  customer_name: string | null
  packed_by_name: string | null
  packed_at: string | null
  out_scanned_at: string | null
  out_scanned_by_name: string | null
}

export type Batch = {
  batch_id: string
  batch_code: string
  status: 'open' | 'scanning' | 'complete' | 'released' | 'cancelled'
  planned_carton_count: number
  assigned_cartons: number
  scanned_cartons: number
  remaining_cartons: number
  created_at: string
  created_by_name: string | null
  released_at: string | null
  released_by_name: string | null
  notes: string | null
  cartons: Carton[]
  message: string
}

export type BatchCompleteResult = {
  batch: Batch
  completed: boolean
  message: string
}

// --- Phase 4: pickup and gate exit -----------------------------------------

export type AwaitingPickup = {
  batch_id: string
  batch_code: string
  released_at: string | null
  carton_count: number
  released_by_name: string | null
}

export type PickupCarton = {
  invoice_id: string
  invoice_number: string
  sku: string
  units: number
  customer_name: string | null
  packed_by_name: string | null
  out_scanned_at: string | null
  exit_scanned_at: string | null
  exit_scanned_by_name: string | null
}

export type Pickup = {
  pickup_id: string
  pickup_code: string
  status:
    | 'registered'
    | 'verifying'
    | 'verified'
    /** CP7 passed, waiting on an Admin decision before the gate opens. */
    | 'exit_pending'
    | 'departed'
    | 'cancelled'
  vehicle_number: string
  courier_name: string | null
  transporter_name: string | null
  batch_id: string
  batch_code: string
  released_cartons: number
  verified_cartons: number
  remaining_cartons: number
  registered_at: string
  registered_by_name: string | null
  verified_at: string | null
  verified_by_name: string | null
  time_in: string | null
  time_out: string | null
  released_by_name: string | null
  exit_requested_at: string | null
  exit_requested_by_name: string | null
  exit_approved_at: string | null
  exit_approved_by_name: string | null
  exit_rejected_note: string | null
  exit_waiting_seconds: number | null
  message: string
  persons: Person[]
  cartons: PickupCarton[]
}

export type PickupVerifyResult = {
  pickup: Pickup
  verified: boolean
  message: string
}

export type Dashboard = {
  counters: {
    pending_approvals: number
    sla_breached: number
    open_exceptions: number
    held_boxes: number
    trucks_today: number
    trucks_onsite: number
    scans_today: number
    boxes_closed_today: number
  }
  activity: {
    id: string
    entry_code: string
    status: string
    vehicle_number: string
    vendor_name: string
    requested_at: string | null
    time_in: string | null
    declared_box_count: number | null
    held_boxes: number
    waiting_seconds: number | null
  }[]
  open_exceptions: {
    id: string
    exception_code: string
    title: string
    status: string
    exception_type: string
    vendor_name: string | null
    reported_at: string
  }[]
}

// --- Admin (PRD §2 "Admin", §8) --------------------------------------------

export type Staff = {
  id: string
  full_name: string
  employee_code: string | null
  role: string
  role_label: string
  is_active: boolean
  is_backup_approver: boolean
  /** Whether a badge was ever issued. The code itself is never returned. */
  has_badge: boolean
  badge_active: boolean
  badge_usable: boolean
  can_hold_badge: boolean
  invoices_verified: number
  cartons_packed: number
  last_attributed_at: string | null
  created_at: string
}

export type StaffCreated = {
  staff: Staff
  /** Shown once. It exists nowhere else afterwards, including on the server. */
  temporary_password: string
}

export type BadgeIssued = {
  staff: Staff
  /**
   * The only badge code the frontend ever sees, for the badge just minted, for
   * as long as this screen is open. There is no endpoint that reads one back —
   * see docs/DECISIONS.md §CC2.
   */
  badge_code: string
}

export type RoleOption = {
  value: string
  label: string
  carries_badge: boolean
}

export type AdminMeta = {
  roles: RoleOption[]
}

export type AccountHistoryEntry = {
  occurred_at: string
  action: 'INSERT' | 'UPDATE'
  changed_keys: string[] | null
  actor_source: string
  actor_role: string | null
  actor_name: string | null
}

export type PhotoRetention = {
  photos_held: number
  photos_purged: number
  oldest_held_at: string | null
  last_purge_at: string | null
  /** Held past the window. Above zero and staying there means the job stopped. */
  overdue: number
  /** Deliberately kept: a block is enforced on a mobile number, not a face. */
  retained_for_block: number
  retention_days: number
  enabled: boolean
}

// --- Phase 5 workflow: assignment, load approval, exit approval -------------

export type PackingState = {
  invoice_id: string
  invoice_number: string
  sku: string | null
  required_units: number
  packed_units: number
  remaining_units: number
  ready_to_close: boolean
  is_open: boolean
  verified_by: string | null
  verified_by_name: string | null
  assigned_to: string | null
  assigned_to_name: string | null
  packed_by: string | null
  packed_by_name: string | null
  packed_at: string | null
}

export type AssignResult = {
  invoice: Invoice
  packing: PackingState
  assigned_to: BadgeHolder
  message: string
}

/** A product-box scan, plus where the carton now stands. */
export type PackScanResult = ScanResult & {
  packed_units?: number
  required_units?: number
  remaining_units?: number
  ready_to_close?: boolean
}

/** How many units have been confirmed at matching, before the badge scan. */
export type MatchingState = {
  invoice_id: string
  invoice_number: string
  sku: string | null
  required_units: number
  matched_units: number
  remaining_units: number
  ready_to_verify: boolean
  is_open: boolean
  verified_by: string | null
  verified_by_name: string | null
}

/** A unit-sticker scan at matching, plus where the invoice now stands. */
export type MatchScanResult = ScanResult & {
  matched_units?: number
  required_units?: number
  remaining_units?: number
  ready_to_verify?: boolean
}

export type BatchAwaitingCount = {
  batch_id: string
  batch_code: string
  batch_status: string
  planned_carton_count: number
  carton_count: number
  created_at: string
}

export type LoadApproval = {
  id: string
  batch_id: string
  batch_code: string
  batch_status: string
  counted_cartons: number
  expected_cartons: number
  counted_by: string | null
  counted_by_name: string | null
  counted_at: string
  status: 'pending' | 'approved' | 'rejected'
  decided_by: string | null
  decided_by_name: string | null
  decided_at: string | null
  note: string | null
  is_current: boolean
  /** Whether the guard's number agreed with the system's. */
  matches: boolean
  waiting_seconds: number | null
}

export type ExitRequestResult = {
  pickup: Pickup
  requested: boolean
  message: string
}

export type ExitDecisionResult = {
  pickup: Pickup
  approved: boolean
  message: string
}
