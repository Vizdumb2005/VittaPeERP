frappe.ui.form.on("Approval Request", {
	refresh(frm) {
		const is_checker = frm.doc.checker === frappe.session.user;
		const is_pending = frm.doc.status === "Pending";

		if (!(is_checker && is_pending)) {
			return;
		}

		frm.add_custom_button(__("Approve"), () => {
			review_request(frm, "approve");
		}, __("Review"));

		frm.add_custom_button(__("Reject"), () => {
			frappe.prompt(
				[
					{
						label: "Reason",
						fieldname: "reason",
						fieldtype: "Small Text",
						reqd: 1,
					},
				],
				(values) => review_request(frm, "reject", values.reason),
				__("Reject Approval Request"),
				__("Submit")
			);
		}, __("Review"));
	},
});

function review_request(frm, action, reason = null) {
	frappe.call({
		method: "erpnext.payroll.doctype.approval_request.approval_request.review",
		args: {
			name: frm.doc.name,
			action,
			reason,
		},
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}
