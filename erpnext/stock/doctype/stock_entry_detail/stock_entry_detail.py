# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _, bold
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import (
	cint,
	cstr,
	flt,
	format_time,
	formatdate,
	get_link_to_form,
	getdate,
	nowdate,
)

from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	OpeningEntryAccountError,
)
from erpnext.stock.stock_ledger import NegativeStockError, get_previous_sle, is_negative_stock_allowed

source_mandatory = [
	"Material Issue",
	"Material Transfer",
	"Send to Subcontractor",
	"Material Transfer for Manufacture",
	"Material Consumption for Manufacture",
	"Return Raw Material to Customer",
	"Subcontracting Delivery",
]

target_mandatory = [
	"Material Receipt",
	"Material Transfer",
	"Send to Subcontractor",
	"Material Transfer for Manufacture",
	"Receive from Customer",
	"Subcontracting Return",
]


class StockEntryDetail(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		actual_qty: DF.Float
		additional_cost: DF.Currency
		against_fg: DF.Link | None
		against_stock_entry: DF.Link | None
		allow_alternative_item: DF.Check
		allow_zero_valuation_rate: DF.Check
		amount: DF.Currency
		barcode: DF.Data | None
		basic_amount: DF.Currency
		basic_rate: DF.Currency
		batch_no: DF.Link | None
		bom_no: DF.Link | None
		bom_secondary_item: DF.Data | None
		conversion_factor: DF.Float
		cost_center: DF.Link | None
		customer_provided_item_cost: DF.Currency
		description: DF.TextEditor | None
		expense_account: DF.Link | None
		has_item_scanned: DF.Check
		image: DF.Attach | None
		is_finished_item: DF.Check
		is_legacy_scrap_item: DF.Check
		item_code: DF.Link
		item_group: DF.Data | None
		item_name: DF.Data | None
		job_card_item: DF.Data | None
		landed_cost_voucher_amount: DF.Currency
		material_request: DF.Link | None
		material_request_item: DF.Link | None
		original_item: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		po_detail: DF.Data | None
		project: DF.Link | None
		putaway_rule: DF.Link | None
		qty: DF.Float
		quality_inspection: DF.Link | None
		reference_purchase_receipt: DF.Link | None
		retain_sample: DF.Check
		s_warehouse: DF.Link | None
		sample_quantity: DF.Int
		scio_detail: DF.Data | None
		sco_rm_detail: DF.Data | None
		serial_and_batch_bundle: DF.Link | None
		serial_no: DF.Text | None
		set_basic_rate_manually: DF.Check
		ste_detail: DF.Data | None
		stock_uom: DF.Link
		subcontracted_item: DF.Link | None
		t_warehouse: DF.Link | None
		transfer_qty: DF.Float
		transferred_qty: DF.Float
		type: DF.Literal["", "Co-Product", "By-Product", "Scrap", "Additional Finished Good"]
		uom: DF.Link
		use_serial_batch_fields: DF.Check
		valuation_rate: DF.Currency
	# end: auto-generated types

	def validate_batch(self):
		if not self.batch_no:
			return

		disabled = frappe.db.get_value("Batch", self.batch_no, "disabled")
		if disabled:
			frappe.throw(_("Batch {0} of Item {1} is disabled.").format(self.batch_no, self.item_code))
			return

		expiry_date = frappe.db.get_value("Batch", self.batch_no, "expiry_date")
		if expiry_date and getdate(self.parent_doc.posting_date) > getdate(expiry_date):
			frappe.throw(_("Batch {0} of Item {1} has expired.").format(self.batch_no, self.item_code))

	def validate_and_update_item_details(self, item_details, company, purpose):
		if flt(self.qty) and flt(self.qty) < 0:
			frappe.throw(
				_("Row {0}: The item {1}, quantity must be positive number").format(
					self.idx, bold(self.item_code)
				)
			)

		if item_details.get("is_stock_item") != 1:
			frappe.throw(_("{0} is not a stock Item").format(self.item_code))

		reset_fields = ("stock_uom", "item_name")
		for field in reset_fields:
			self.set(field, item_details.get(field))

		update_fields = (
			"uom",
			"description",
			"expense_account",
			"cost_center",
			"conversion_factor",
			"barcode",
		)
		for field in update_fields:
			if not self.get(field):
				self.set(field, item_details.get(field))
			if field == "conversion_factor" and self.uom == item_details.get("stock_uom"):
				self.set(field, item_details.get(field))

		if not self.transfer_qty and self.qty:
			self.transfer_qty = flt(
				flt(self.qty) * flt(self.conversion_factor), self.precision("transfer_qty")
			)

		if purpose == "Subcontracting Delivery":
			self.expense_account = frappe.get_value("Company", company, "default_expense_account")

	def validate_expense_account(self, is_opening, purpose):
		if not self.expense_account:
			frappe.throw(
				_(
					"Please enter <b>Difference Account</b> or set default "
					"<b>Stock Adjustment Account</b> for company {0}"
				).format(bold(self.parent_doc.company))
			)

		acc_details = frappe.get_cached_value(
			"Account",
			self.expense_account,
			["account_type", "report_type"],
			as_dict=True,
		)

		if is_opening == "Yes" and acc_details.report_type == "Profit and Loss":
			frappe.throw(
				_(
					"Difference Account must be a Asset/Liability type account "
					"(Temporary Opening), since this Stock Entry is an Opening Entry"
				),
				OpeningEntryAccountError,
			)

		if acc_details.account_type == "Stock":
			frappe.throw(
				_("At row #{0}: the Difference Account must not be a Stock type account...").format(
					self.idx, get_link_to_form("Account", self.expense_account)
				),
				title=_("Difference Account in Items Table"),
			)

		if (
			purpose not in ["Material Issue", "Subcontracting Delivery"]
			and acc_details.account_type == "Cost of Goods Sold"
		):
			frappe.msgprint(
				_("At row #{0}: you have selected the Difference Account {1}...").format(
					self.idx, bold(get_link_to_form("Account", self.expense_account))
				),
				indicator="orange",
				alert=1,
			)

	def set_transfer_qty(self):
		if not flt(self.conversion_factor):
			frappe.throw(_("Row {0}: UOM Conversion Factor is mandatory").format(self.idx))

		self.transfer_qty = flt(flt(self.qty) * flt(self.conversion_factor), self.precision("transfer_qty"))

		if not flt(self.transfer_qty):
			frappe.throw(
				_("Row {0}: Qty in Stock UOM can not be zero.").format(self.idx), title=_("Zero quantity")
			)

	def remove_warehouse_if_not_required(self, parent_doc):
		if parent_doc.purpose in source_mandatory and parent_doc.purpose not in target_mandatory:
			parent_doc.to_warehouse = None
			self.t_warehouse = None
		elif parent_doc.purpose in target_mandatory and parent_doc.purpose not in source_mandatory:
			parent_doc.from_warehouse = None
			self.s_warehouse = None

	def set_warehouse_based_on_defaults(self, parent_doc):
		if not self.s_warehouse and not self.t_warehouse:
			self.s_warehouse = parent_doc.from_warehouse
			self.t_warehouse = parent_doc.to_warehouse

		if parent_doc.purpose in source_mandatory and not self.s_warehouse:
			if parent_doc.from_warehouse:
				self.s_warehouse = parent_doc.from_warehouse

		if parent_doc.purpose in target_mandatory and not self.t_warehouse:
			if parent_doc.to_warehouse:
				self.t_warehouse = parent_doc.to_warehouse

		if parent_doc.purpose == "Manufacture" and parent_doc.bom_no:
			if self.is_finished_item or self.type or self.is_legacy_scrap_item:
				self.s_warehouse = None
			else:
				self.t_warehouse = None

		if parent_doc.purpose == "Disassemble" and parent_doc.bom_no:
			if self.is_finished_item or self.type or self.is_legacy_scrap_item:
				self.t_warehouse = None
			else:
				self.s_warehouse = None

	def validate_warehouse(self, parent_doc):
		if parent_doc.purpose in source_mandatory and not self.s_warehouse:
			frappe.throw(_("Source warehouse is mandatory for row {0}").format(self.idx))

		if parent_doc.purpose in target_mandatory and not self.t_warehouse:
			frappe.throw(_("Target warehouse is mandatory for row {0}").format(self.idx))

		if parent_doc.purpose == "Manufacture" and parent_doc.bom_no:
			if self.is_finished_item or self.type or self.is_legacy_scrap_item:
				if not self.t_warehouse:
					frappe.throw(_("Target warehouse is mandatory for row {0}").format(self.idx))
			else:
				if not self.s_warehouse:
					frappe.throw(_("Source warehouse is mandatory for row {0}").format(self.idx))

		if parent_doc.purpose == "Disassemble" and parent_doc.bom_no:
			if self.is_finished_item or self.type or self.is_legacy_scrap_item:
				if not self.s_warehouse:
					frappe.throw(_("Source warehouse is mandatory for row {0}").format(self.idx))
			else:
				if not self.t_warehouse:
					frappe.throw(_("Target warehouse is mandatory for row {0}").format(self.idx))

		if cstr(self.s_warehouse) == cstr(self.t_warehouse) and parent_doc.purpose not in [
			"Material Transfer for Manufacture",
			"Material Transfer",
		]:
			frappe.throw(_("Source and target warehouse cannot be same for row {0}").format(self.idx))

		if not (self.s_warehouse or self.t_warehouse):
			frappe.throw(_("At least one warehouse is mandatory"))

	def set_actual_qty(self, posting_date, posting_time):
		allow_negative_stock = is_negative_stock_allowed(item_code=self.item_code)
		previous_sle = get_previous_sle(
			{
				"item_code": self.item_code,
				"warehouse": self.s_warehouse or self.t_warehouse,
				"posting_date": posting_date,
				"posting_time": posting_time,
			}
		)

		# get actual stock at source warehouse
		self.actual_qty = previous_sle.get("qty_after_transaction") or 0

		# validate qty during submit
		if (
			self.docstatus == 1
			and self.s_warehouse
			and not allow_negative_stock
			and flt(self.actual_qty, self.precision("actual_qty"))
			< flt(self.transfer_qty, self.precision("actual_qty"))
		):
			frappe.throw(
				_(
					"Row {0}: Quantity not available for {4} in warehouse {1} at posting time of the entry ({2} {3})"
				).format(
					self.idx,
					bold(self.s_warehouse),
					formatdate(posting_date),
					format_time(posting_time),
					bold(self.item_code),
				)
				+ "<br><br>"
				+ _("Available quantity is {0}, you need {1}").format(
					bold(flt(self.actual_qty, self.precision("actual_qty"))),
					bold(self.transfer_qty),
				),
				NegativeStockError,
				title=_("Insufficient Stock"),
			)

	def get_total_supplied_qty(self, parent_doc):
		se = frappe.qb.DocType("Stock Entry")
		se_detail = frappe.qb.DocType("Stock Entry Detail")

		return (
			frappe.qb.from_(se)
			.inner_join(se_detail)
			.on(se.name == se_detail.parent)
			.select(Sum(se_detail.transfer_qty))
			.where(
				(se.purpose == "Send to Subcontractor")
				& (se.docstatus == 1)
				& (se_detail.item_code == self.item_code)
				& (
					(
						(se.purchase_order == parent_doc.purchase_order)
						& (se_detail.po_detail == parent_doc.po_detail)
					)
					if parent_doc.subcontract_data.order_doctype == "Purchase Order"
					else (
						(se.subcontracting_order == parent_doc.subcontracting_order)
						& (se_detail.sco_rm_detail == self.sco_rm_detail)
					)
				)
			)
		).run()[0][0] or 0

	def get_total_returned_qty(self, parent_doc):
		se = frappe.qb.DocType("Stock Entry")
		se_detail = frappe.qb.DocType("Stock Entry Detail")

		return (
			frappe.qb.from_(se)
			.inner_join(se_detail)
			.on(se.name == se_detail.parent)
			.select(Sum(se_detail.transfer_qty))
			.where(
				(se.purpose == "Material Transfer")
				& (se.docstatus == 1)
				& (se.is_return == 1)
				& (se_detail.item_code == self.item_code)
				& (se_detail.sco_rm_detail == self.sco_rm_detail)
				& (se.subcontracting_order == parent_doc.subcontracting_order)
			)
		).run()[0][0] or 0

	def get_order_rm_detail(self, parent_doc):
		filters = {
			"parent": parent_doc.get(parent_doc.subcontract_data.order_field),
			"docstatus": 1,
			"rm_item_code": self.item_code,
			"main_item_code": self.subcontracted_item,
		}

		return frappe.db.get_value(parent_doc.subcontract_data.order_supplied_items_field, filters, "name")

	def validate_subcontracting_order_for_bom(self, subcontract_order, parent_doc):
		def get_required_qty(item_code):
			return sum(
				flt(d.required_qty) for d in subcontract_order.supplied_items if d.rm_item_code == item_code
			)

		qty_allowance = flt(frappe.db.get_single_value("Buying Settings", "over_transfer_allowance"))
		item_code = self.original_item or self.item_code
		required_qty = get_required_qty(item_code)

		if not required_qty and self.allow_alternative_item:
			original_item_code = frappe.get_value(
				"Item Alternative", {"alternative_item_code": item_code}, "item_code"
			)
			required_qty = get_required_qty(original_item_code)

		if not required_qty:
			frappe.throw(
				_("Item {0} not found in 'Raw Materials Supplied' table in {1} {2}").format(
					self.item_code,
					parent_doc.subcontract_data.order_doctype,
					parent_doc.get(parent_doc.subcontract_data.order_field),
				)
			)

		total_allowed = required_qty + (required_qty * (qty_allowance / 100))
		total_supplied = self.get_total_supplied_qty(parent_doc)

		total_returned = 0
		if parent_doc.subcontract_data.order_doctype == "Subcontracting Order":
			total_returned = self.get_total_returned_qty(parent_doc)

		if flt(total_supplied + self.transfer_qty - total_returned, self.precision("transfer_qty")) > flt(
			total_allowed, self.precision("transfer_qty")
		):
			frappe.throw(
				_("Row #{0}: Item {1} cannot be transferred more than {2} against {3} {4}").format(
					self.idx,
					self.item_code,
					total_allowed,
					parent_doc.subcontract_data.order_doctype,
					parent_doc.get(parent_doc.subcontract_data.order_field),
				)
			)
		elif not self.get(parent_doc.subcontract_data.rm_detail_field):
			order_rm_detail = self.get_order_rm_detail(parent_doc)
			if order_rm_detail:
				self.db_set(parent_doc.subcontract_data.rm_detail_field, order_rm_detail)
			else:
				if not self.allow_alternative_item:
					frappe.throw(
						_("Row {0}# Item {1} not found in 'Raw Materials Supplied' table in {2} {3}").format(
							self.idx,
							self.item_code,
							parent_doc.subcontract_data.order_doctype,
							parent_doc.get(parent_doc.subcontract_data.order_field),
						)
					)

	def validate_subcontracting_order_for_transfer(self, parent_doc):
		if not self.subcontracted_item:
			frappe.throw(
				_("Row {0}: Subcontracted Item is mandatory for the raw material {1}").format(
					self.idx, bold(self.item_code)
				)
			)
		elif not self.get(parent_doc.subcontract_data.rm_detail_field):
			order_rm_detail = self.get_order_rm_detail(parent_doc)
			if order_rm_detail:
				self.db_set(parent_doc.subcontract_data.rm_detail_field, order_rm_detail)

	def get_material_request(self, purpose, outgoing_stock_entry):
		material_request = self.material_request or None
		material_request_item = self.material_request_item or None

		if purpose == "Material Transfer" and outgoing_stock_entry:
			parent_se = frappe.get_value(
				"Stock Entry Detail",
				self.ste_detail,
				["material_request", "material_request_item"],
				as_dict=True,
			)
			if parent_se:
				material_request = parent_se.material_request
				material_request_item = parent_se.material_request_item

		return material_request, material_request_item

	def validate_material_request(self, purpose, outgoing_stock_entry):
		material_request, material_request_item = self.get_material_request(purpose, outgoing_stock_entry)
		if not material_request:
			return

		mreq_item = frappe.db.get_value(
			"Material Request Item",
			{"name": material_request_item, "parent": material_request},
			["item_code", "warehouse", "idx"],
			as_dict=True,
		)

		if mreq_item.item_code != self.item_code:
			frappe.throw(
				_("Item for row {0} does not match Material Request").format(self.idx),
				frappe.MappingMismatchError,
			)

	def delink_asset_repair_sabb(self, asset_repair):
		if not self.serial_and_batch_bundle:
			return

		voucher_detail_no = frappe.db.get_value(
			"Asset Repair Consumed Item",
			{"parent": asset_repair, "serial_and_batch_bundle": self.serial_and_batch_bundle},
			"name",
		)

		if not voucher_detail_no:
			return

		doc = frappe.get_doc("Serial and Batch Bundle", self.serial_and_batch_bundle)
		doc.db_set(
			{
				"voucher_type": "Asset Repair",
				"voucher_no": asset_repair,
				"voucher_detail_no": voucher_detail_no,
			}
		)


def get_transferred_qty(material_request):
	sed = frappe.qb.DocType("Stock Entry Detail")

	query = (
		frappe.qb.from_(sed)
		.select(
			Sum(sed.transfer_qty).as_("transfer_qty"),
			Sum(sed.transferred_qty).as_("transferred_qty"),
		)
		.where((sed.material_request == material_request) & (sed.docstatus == 1))
	).run(as_dict=True)

	return query[0]
