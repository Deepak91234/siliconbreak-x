// CircuitLoop preverified library: rising-edge detector.
// 'pulse' is high for exactly one cycle after 'd' goes from 0 to 1.
// d must already be synchronized to clk.
`default_nettype none
module cl_edge_rise (
    input  wire clk,
    input  wire d,
    output wire pulse
);
    reg prev = 1'b0;

    always @(posedge clk) begin
        prev <= d;
    end

    assign pulse = d & ~prev;
endmodule
`default_nettype wire
