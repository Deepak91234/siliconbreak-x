// SiliconBreak X preverified library: counter-based debouncer.
// q follows d only after d has differed from q for STABLE_CYCLES consecutive
// clock cycles, so bounces shorter than STABLE_CYCLES are rejected.
// d must already be synchronized to clk (see cl_sync2 / cl_input_cond).
`default_nettype none
module cl_debounce #(
    parameter integer STABLE_CYCLES = 1000000,
    parameter [0:0]   INIT          = 1'b0
) (
    input  wire clk,
    input  wire d,
    output wire q
);
    localparam integer CW = (STABLE_CYCLES > 2) ? $clog2(STABLE_CYCLES) : 1;
    /* verilator lint_off WIDTHTRUNC */
    localparam [CW-1:0] LAST = STABLE_CYCLES - 1;
    /* verilator lint_on WIDTHTRUNC */

    reg          state = INIT;
    reg [CW-1:0] cnt   = {CW{1'b0}};

    always @(posedge clk) begin
        if (d == state) begin
            cnt <= {CW{1'b0}};
        end else if (cnt == LAST) begin
            state <= d;
            cnt   <= {CW{1'b0}};
        end else begin
            cnt <= cnt + 1'b1;
        end
    end

    assign q = state;
endmodule
`default_nettype wire
