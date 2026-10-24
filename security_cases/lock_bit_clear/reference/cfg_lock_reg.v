// Configuration register with a software-settable lock.
//
//   addr 0  CONFIG   8-bit configuration value (drives config_out)
//   addr 1  CONTROL  bit 0 = LOCK
//
// Writes take effect on the clock edge that samples wr_en. Once software sets
// LOCK, the configuration must stay frozen until the next reset.
`default_nettype none
module cfg_lock_reg (
    input  wire       clk,
    input  wire       reset,        // synchronous, active high
    input  wire       wr_en,
    input  wire       wr_addr,      // 0 = CONFIG, 1 = CONTROL
    input  wire [7:0] wr_data,
    output wire [7:0] config_out,
    output wire       locked
);
    localparam ADDR_CONFIG  = 1'b0;
    localparam ADDR_CONTROL = 1'b1;

    reg [7:0] config_q;
    reg       lock_q;

    always @(posedge clk) begin
        if (reset) begin
            config_q <= 8'h00;
            lock_q   <= 1'b0;
        end else if (wr_en) begin
            if (wr_addr == ADDR_CONTROL) begin
                // LOCK is sticky: software can set it, only reset clears it
                lock_q <= lock_q | wr_data[0];
            end else if (!lock_q) begin
                config_q <= wr_data;
            end
        end
    end

    assign config_out = config_q;
    assign locked     = lock_q;
endmodule
`default_nettype wire
