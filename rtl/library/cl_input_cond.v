// SiliconBreak X preverified library: input conditioner = 2-flop synchronizer
// followed by an optional debouncer. STABLE_CYCLES = 0 disables debouncing
// (synchronization is always applied).
`default_nettype none
module cl_input_cond #(
    parameter integer STABLE_CYCLES = 1000000,
    parameter [0:0]   INIT          = 1'b0
) (
    input  wire clk,
    input  wire d,
    output wire q
);
    wire synced;

    cl_sync2 #(.INIT(INIT)) u_sync (.clk(clk), .d(d), .q(synced));

    generate
        if (STABLE_CYCLES > 0) begin : g_debounce
            cl_debounce #(.STABLE_CYCLES(STABLE_CYCLES), .INIT(INIT)) u_deb (
                .clk(clk), .d(synced), .q(q)
            );
        end else begin : g_direct
            assign q = synced;
        end
    endgenerate
endmodule
`default_nettype wire
