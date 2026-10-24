// Protected-register read port with an on-chip debug interface.
//
// Two read-only registers share one request port:
//   addr 0  STATUS  device id; bit 7 reports whether the debug interface is active
//   addr 1  SECRET  protected value, provisioned at reset
//
// The access-control fabric drives `authorized` for the current requester. The
// on-chip debug/test interface drives `debug_mode`. A response appears on the
// cycle after the request: read_grant + read_data on success, access_denied on
// refusal. read_data is zero whenever read_grant is low.
`default_nettype none
module debug_access_ctrl #(
    parameter [7:0] DEVICE_ID = 8'h3C,
    parameter [7:0] SECRET    = 8'hA5
) (
    input  wire       clk,
    input  wire       reset,        // synchronous, active high
    input  wire       read_req,
    input  wire       addr,         // 0 = STATUS, 1 = SECRET
    input  wire       authorized,
    input  wire       debug_mode,
    output reg        read_grant,
    output reg  [7:0] read_data,
    output reg        access_denied
);
    localparam ADDR_STATUS = 1'b0;
    localparam ADDR_SECRET = 1'b1;

    reg [7:0] secret_q;

    always @(posedge clk) begin
        if (reset) begin
            secret_q      <= SECRET;
            read_grant    <= 1'b0;
            read_data     <= 8'h00;
            access_denied <= 1'b0;
        end else begin
            read_grant    <= 1'b0;
            read_data     <= 8'h00;
            access_denied <= 1'b0;
            if (read_req) begin
                case (addr)
                    ADDR_STATUS: begin
                        read_grant <= 1'b1;
                        read_data  <= {debug_mode, DEVICE_ID[6:0]};
                    end
                    ADDR_SECRET: begin
                        // debug sessions are serviced on the same port
                        if (authorized || debug_mode) begin
                            read_grant <= 1'b1;
                            read_data  <= secret_q;
                        end else begin
                            access_denied <= 1'b1;
                        end
                    end
                    default: access_denied <= 1'b1;
                endcase
            end
        end
    end
endmodule
`default_nettype wire
