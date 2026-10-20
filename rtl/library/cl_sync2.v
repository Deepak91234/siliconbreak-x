// CircuitLoop preverified library: two-flop synchronizer.
// Brings an asynchronous single-bit input (button, switch) into the clk domain.
`default_nettype none
module cl_sync2 #(
    parameter [0:0] INIT = 1'b0
) (
    input  wire clk,
    input  wire d,
    output wire q
);
    (* ASYNC_REG = "TRUE" *) reg s1 = INIT;
    (* ASYNC_REG = "TRUE" *) reg s2 = INIT;

    always @(posedge clk) begin
        s1 <= d;
        s2 <= s1;
    end

    assign q = s2;
endmodule
`default_nettype wire
