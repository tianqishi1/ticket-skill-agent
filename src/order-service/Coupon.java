package com.example.order.coupon;

import lombok.Data;

/**
 * 优惠券实体（示意，不涉及本次故障根因）
 */
@Data
public class Coupon {
    private Long id;
    private Long tenantId;
    private String name;
    private Integer type;
    // discount/minAmount/startTime/endTime/status ...
}
