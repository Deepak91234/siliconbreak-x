// TRUSTED formal property wrapper for the debug_bypass_demo case (SiliconBreak X).
// Read by Yosys with `read_verilog -formal`; proven with Yosys's built-in SAT
// solver (bounded check + temporal induction). Never shown to a repair agent.
//
// P-NOLEAK: one cycle after an unauthorized SECRET read (any debug_mode), the
// controller must not assert read_grant, must drive zero on read_data, and must
// assert access_denied.
module debug_access_ctrl_formal (
    input wire clk,
    input wire reset,
    input wire read_req,
    input wire addr,
    input wire authorized,
    input wire debug_mode
);
    wire       read_grant;
    wire [7:0] read_data;
    wire       access_denied;

    debug_access_ctrl dut (
        .clk(clk), .reset(reset), .read_req(read_req), .addr(addr),
        .authorized(authorized), .debug_mode(debug_mode),
        .read_grant(read_grant), .read_data(read_data), .access_denied(access_denied)
    );

    reg past_valid = 1'b0;
    reg prev_unauth_secret = 1'b0;

    always @(posedge clk) begin
        past_valid         <= 1'b1;
        prev_unauth_secret <= !reset && read_req && addr && !authorized;
    end

    always @(*) begin
        if (!past_valid) assume (reset);
        if (past_valid && prev_unauth_secret) begin
            assert (!read_grant);
            assert (read_data == 8'h00);
            assert (access_denied);
        end
    end
endmodule
