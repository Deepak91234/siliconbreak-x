// TRUSTED Basys 3 wrapper for the debug_bypass_demo case (SiliconBreak X).
// Hand-written; the port list matches CircuitLoop's generated wrappers, so the
// pins come from boards/basys3/board.json through the standard XDC generator.
//
//   BTNU  read_req (hold)          SW1  authorized (simulated permission input)
//   BTNC  reset                    SW2  debug_mode
//                                  SW3  addr: off = STATUS, on = SECRET
//   LED7..LED0  read_data   LED15  read_grant   LED14  access_denied
//
// The controller is reset for 16 cycles after configuration, so SECRET is
// provisioned without pressing BTNC.
`default_nettype none
module basys3_top #(
    parameter integer DEBOUNCE_CYCLES = 1000000  // 10 ms at 100 MHz
) (
    input  wire        clk,
    input  wire [15:0] sw,
    input  wire        btnC,
    input  wire        btnU,
    input  wire        btnL,
    input  wire        btnR,
    input  wire        btnD,
    output wire [15:0] led,
    output wire [6:0]  seg,
    output wire        dp,
    output wire [3:0]  an
);
    wire read_req, reset_btn, authorized, debug_mode, addr;
    cl_input_cond #(.STABLE_CYCLES(DEBOUNCE_CYCLES)) u_in_req  (.clk(clk), .d(btnU),  .q(read_req));
    cl_input_cond #(.STABLE_CYCLES(DEBOUNCE_CYCLES)) u_in_rst  (.clk(clk), .d(btnC),  .q(reset_btn));
    cl_input_cond #(.STABLE_CYCLES(DEBOUNCE_CYCLES)) u_in_auth (.clk(clk), .d(sw[1]), .q(authorized));
    cl_input_cond #(.STABLE_CYCLES(DEBOUNCE_CYCLES)) u_in_dbg  (.clk(clk), .d(sw[2]), .q(debug_mode));
    cl_input_cond #(.STABLE_CYCLES(DEBOUNCE_CYCLES)) u_in_addr (.clk(clk), .d(sw[3]), .q(addr));

    reg [4:0] por_count = 5'd0;
    wire      por_active = ~por_count[4];
    always @(posedge clk) begin
        if (por_active) por_count <= por_count + 5'd1;
    end

    wire       read_grant, access_denied;
    wire [7:0] read_data;
    debug_access_ctrl u_ctrl (
        .clk(clk), .reset(reset_btn | por_active), .read_req(read_req), .addr(addr),
        .authorized(authorized), .debug_mode(debug_mode),
        .read_grant(read_grant), .read_data(read_data), .access_denied(access_denied)
    );

    assign led[7:0]  = read_data;
    assign led[13:8] = 6'b000000;
    assign led[14]   = access_denied;
    assign led[15]   = read_grant;

    assign seg = 7'b1111111;
    assign dp  = 1'b1;
    assign an  = 4'b1111;

    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused_inputs = &{1'b0, sw[0], sw[15:4], btnL, btnR, btnD};
    /* verilator lint_on UNUSEDSIGNAL */
endmodule
`default_nettype wire
