// SiliconBreak X preverified library: programmable clock-enable generator.
// Emits a one-cycle 'tick' every 'period' enabled clock cycles (period >= 1).
// The period may change at run time; if the running count already exceeds the
// new period the next tick fires immediately. 'clear' restarts the interval.
// Use the tick as a clock enable -- never as a clock.
`default_nettype none
module cl_tick_div #(
    parameter integer WIDTH = 32
) (
    input  wire             clk,
    input  wire             clear,
    input  wire             enable,
    input  wire [WIDTH-1:0] period,
    output wire             tick
);
    reg [WIDTH-1:0] cnt   = {WIDTH{1'b0}};
    reg             pulse = 1'b0;

    always @(posedge clk) begin
        pulse <= 1'b0;
        if (clear) begin
            cnt <= {WIDTH{1'b0}};
        end else if (enable) begin
            if (cnt >= period - 1'b1) begin
                cnt   <= {WIDTH{1'b0}};
                pulse <= 1'b1;
            end else begin
                cnt <= cnt + 1'b1;
            end
        end
    end

    assign tick = pulse;
endmodule
`default_nettype wire
