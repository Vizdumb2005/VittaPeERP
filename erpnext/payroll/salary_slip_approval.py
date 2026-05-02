import frappe


def validate_salary_slip_approval(doc, method=None):
	"""Block Salary Slip save unless linked Approval Request is approved."""
	approval = frappe.get_all(
		"Approval Request",
		filters={
			"request_type": "Salary Slip",
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
		},
		fields=["name", "status"],
		order_by="creation desc",
		limit=1,
	)

	if not approval:
		frappe.throw(
			"Approval Request not found for this Salary Slip. Please create one and get it approved."
		)

	if approval[0].status != "Approved":
		frappe.throw(f"Salary Slip is blocked until approval is granted. Current status: {approval[0].status}")


def create_salary_slip_approval_request(doc, method=None):
	"""Sample integration: create pending Approval Request when Salary Slip is inserted."""
	exists = frappe.db.exists(
		"Approval Request",
		{
			"request_type": "Salary Slip",
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
		},
	)
	if exists:
		return

	checker = frappe.db.get_single_value("HR Settings", "leave_approver") or "Administrator"
	frappe.get_doc(
		{
			"doctype": "Approval Request",
			"request_type": "Salary Slip",
			"payload_json": frappe.as_json(doc.as_dict(no_default_fields=True)),
			"maker": frappe.session.user,
			"checker": checker,
			"status": "Pending",
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
		}
	).insert(ignore_permissions=True)
